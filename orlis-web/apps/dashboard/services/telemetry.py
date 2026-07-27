"""
Processamento de telemetria de chillers.

Migrado de ibis_prototipo/script/scriptV5.py: a leitura Modbus (read_modbus_block)
e o registo em .txt (salvar_txt) ficam de fora — os dados já chegam decodificados
via MQTT e a persistência estruturada é só no MongoDB.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import DecimalField, F
from django.db.models.functions import Coalesce

from apps.dashboard.models import Chiller

logger = logging.getLogger(__name__)

# Apanha qualquer falha do CoolProp, não só ImportError: um wheel incompatível com a
# versão do Python rebenta com TypeError dentro do próprio import (visto com
# CoolProp 6.6.0 em Python 3.14). Sem isto, o import falhado derruba o mqtt_listener
# inteiro no arranque em vez de apenas desligar os cálculos termodinâmicos.
try:
    from CoolProp.CoolProp import PropsSI
    _HAS_COOLPROP = True
except Exception as _coolprop_erro:
    _HAS_COOLPROP = False
    logger.error(
        "CoolProp indisponível (%s: %s) — entalpias, estados do fluido e COP não vão ser "
        "calculados. A telemetria continua a ser recebida e gravada.",
        type(_coolprop_erro).__name__, _coolprop_erro,
    )

DEFAULT_FLUIDO = "R407C"
DEFAULT_MODO = "ARREFECIMENTO"
LIMIAR_CORRENTE_ATIVO = 2.0
IDADE_COMPRESSOR_FATOR = 0.07

MODO_MAP = {'verao': 'ARREFECIMENTO', 'frio': 'ARREFECIMENTO', 'inverno': 'AQUECIMENTO', 'quente': 'AQUECIMENTO'}

# Fuso horário dos chillers (Portugal), para calcular "agora" independentemente do TIME_ZONE
# do Django: as settings têm TIME_ZONE="UTC", e o Django reconfigura a timezone do processo
# inteiro via time.tzset() ao arrancar — isso torna datetime.now() ambíguo (UTC, não Lisboa)
# para qualquer código fora do próprio Django, como este listener. O tarifário e o horímetro
# assumem hora local de Lisboa (tal como o script original, que corria fora do Django).
FUSO_CHILLERS = ZoneInfo("Europe/Lisbon")


def agora_local() -> datetime:
    """Hora atual em Lisboa, naive (sem tzinfo), para comparar/gravar como as strings
    "%Y-%m-%d %H:%M:%S" já usadas em todo este módulo e no MongoDB."""
    return datetime.now(FUSO_CHILLERS).replace(tzinfo=None)

PRECO_TARIFAS = {'ponta': 0.05136, 'cheias': 0.06072, 'vazio': 0.05073, 'super_vazio': 0.05101}
PERIODOS_TARIFARIOS = {
    'verao': {
        'weekday': [(0.0, 0.5, 'cheias'), (0.5, 2.0, 'vazio'), (2.0, 6.0, 'super_vazio'), (6.0, 7.5, 'vazio'), (7.5, 14.0, 'cheias'), (14.0, 17.0, 'ponta'), (17.0, 24.0, 'cheias')],
        'saturday': [(0.0, 3.5, 'vazio'), (3.5, 7.0, 'super_vazio'), (7.0, 10.0, 'vazio'), (10.0, 13.5, 'cheias'), (13.5, 19.5, 'vazio'), (19.5, 23.0, 'cheias'), (23.0, 24.0, 'vazio')],
        'sunday': [(0.0, 4.0, 'vazio'), (4.0, 8.0, 'super_vazio'), (8.0, 24.0, 'vazio')]
    },
    'inverno': {
        'weekday': [(0.0, 0.5, 'cheias'), (0.5, 2.0, 'vazio'), (2.0, 6.0, 'super_vazio'), (6.0, 7.5, 'vazio'), (7.5, 17.0, 'cheias'), (17.0, 22.0, 'ponta'), (22.0, 24.0, 'cheias')],
        'saturday': [(0.0, 3.0, 'vazio'), (3.0, 7.0, 'super_vazio'), (7.0, 10.5, 'vazio'), (10.5, 12.5, 'cheias'), (12.5, 17.3, 'vazio'), (17.3, 22.5, 'cheias'), (22.5, 24.0, 'vazio')],
        'sunday': [(0.0, 4.0, 'vazio'), (4.0, 8.0, 'super_vazio'), (8.0, 24.0, 'vazio')]
    }
}

# Memória de energia por chiller (ip -> {'last_kwh', 'last_time'}), em processo.
# O listener MQTT deve correr como instância única: este estado não é partilhado
# entre processos e não sobrevive a um restart sem init_energy_memory().
memoria_energia = {}

# Timestamp da última leitura processada por chiller (ip -> datetime), em processo.
# A cadência de publicação MQTT ainda não está definida (não se pode assumir 60s
# como no script original) — o horímetro soma o delta real entre leituras consecutivas.
ultima_leitura_por_chiller = {}

# Gap máximo (horas) entre duas leituras do mesmo chiller para ainda somar ao horímetro.
# Acima disto assume-se período sem dados (chiller ou listener offline) e não se credita
# esse intervalo como horas de funcionamento.
MAX_GAP_HORAS = 2.0


# --- Tarifário ---

def _is_horario_verao(dt: datetime) -> bool:
    ano = dt.year
    inicio = max(datetime(ano, 3, d) for d in range(25, 32) if datetime(ano, 3, d).weekday() == 6)
    fim = max(datetime(ano, 10, d) for d in range(25, 32) if datetime(ano, 10, d).weekday() == 6)
    return inicio.date() <= dt.date() < fim.date()


def get_tarifa(dt: datetime):
    dia_semana = dt.weekday()
    hora = dt.hour + dt.minute / 60
    periodo = 'verao' if _is_horario_verao(dt) else 'inverno'
    if dia_semana < 5:
        periods = PERIODOS_TARIFARIOS[periodo]['weekday']
    elif dia_semana == 5:
        periods = PERIODOS_TARIFARIOS[periodo]['saturday']
    else:
        periods = PERIODOS_TARIFARIOS[periodo]['sunday']
    for start, end, ciclo_nome in periods:
        if start <= hora < end:
            return PRECO_TARIFAS[ciclo_nome]
    return PRECO_TARIFAS['vazio']


def calculate_energy_cost(ip: str, energia_total, timestamp_str: str):
    if energia_total is None or energia_total < 0:
        return None, None

    agora = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
    hora_cheia_atual = agora.replace(minute=0, second=0, microsecond=0)

    if ip not in memoria_energia:
        memoria_energia[ip] = {'last_kwh': energia_total, 'last_time': hora_cheia_atual}
        return None, None

    dados_antigos = memoria_energia[ip]

    if hora_cheia_atual > dados_antigos['last_time']:
        memoria_energia[ip] = {'last_kwh': energia_total, 'last_time': hora_cheia_atual}
        return 0.0, 0.0

    delta_kwh = energia_total - dados_antigos['last_kwh']
    if delta_kwh < 0:
        logger.warning(
            "[%s] Contador resetou (%.1f -> %.1f kWh). Baseline atualizada.",
            ip, dados_antigos['last_kwh'], energia_total,
        )
        memoria_energia[ip] = {'last_kwh': energia_total, 'last_time': hora_cheia_atual}
        return 0.0, 0.0
    if delta_kwh >= 800:
        logger.warning("[%s] Delta kWh anómalo (%.1f kWh). Ignorado.", ip, delta_kwh)
        return 0.0, 0.0

    tarifa = get_tarifa(agora)
    return delta_kwh, delta_kwh * tarifa


def _resolver_timestamp_para_calculo(ip: str, timestamp_str: str, recebido_em: datetime) -> str:
    """Valida o timestamp do payload contra a hora do servidor Django no momento da
    receção da mensagem (recebido_em). Se o timestamp for inválido ou o desvio exceder
    MQTT_TIMESTAMP_MAX_SKEW_SECONDS (default 3600s), usa a hora de receção só para os
    cálculos (tarifário e horímetro) desta mensagem — o timestamp original do payload
    mantém-se intacto no documento Mongo, chamado à parte em process_chiller."""
    recebido_str = recebido_em.strftime("%Y-%m-%d %H:%M:%S")
    try:
        payload_dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        logger.warning(
            "[%s] timestamp do payload inválido (%r) — a usar hora de receção (%s) para os cálculos.",
            ip, timestamp_str, recebido_str,
        )
        return recebido_str

    max_skew = getattr(settings, "MQTT_TIMESTAMP_MAX_SKEW_SECONDS", 3600)
    diff_seconds = (payload_dt - recebido_em).total_seconds()
    if abs(diff_seconds) > max_skew:
        logger.warning(
            "[%s] timestamp do payload suspeito: payload=%s receção=%s diff=%.0fs (limite %ss) — "
            "a usar hora de receção para os cálculos; o timestamp original do payload fica guardado no documento.",
            ip, timestamp_str, recebido_str, diff_seconds, max_skew,
        )
        return recebido_str
    return timestamp_str


def _registar_leitura_e_delta_horas(ip: str, timestamp_str: str):
    """Atualiza o relógio da última leitura deste chiller e devolve o delta em horas
    desde a leitura anterior (qualquer estado, standby ou ativo). timestamp_str já vem
    validado por _resolver_timestamp_para_calculo (formato sempre válido).

    Devolve None na primeira leitura de um chiller (sem baseline ainda), se o delta for
    <= 0 (ex.: relógio do Raspberry autocorrigiu-se para trás — trata-se como nova
    baseline, sem somar nem subtrair horas), ou se o gap for anómalo (excede MAX_GAP_HORAS)."""
    agora = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
    anterior = ultima_leitura_por_chiller.get(ip)
    ultima_leitura_por_chiller[ip] = agora  # nova baseline, independentemente do resultado abaixo

    if anterior is None:
        return None

    delta_horas = (agora - anterior).total_seconds() / 3600.0
    if delta_horas <= 0:
        logger.warning(
            "[%s] timestamp não avançou face à leitura anterior (%s -> %s) — possível "
            "autocorreção de relógio. Tratado como nova baseline, sem incremento.",
            ip, anterior, agora,
        )
        return None
    if delta_horas > MAX_GAP_HORAS:
        logger.warning(
            "[%s] gap de %.2fh entre leituras (limite %.1fh) — provável período sem dados; horímetro não soma este intervalo.",
            ip, delta_horas, MAX_GAP_HORAS,
        )
        return None
    return delta_horas


def init_energy_memory(collection, chillers_dict):
    """Restaura memoria_energia a partir do último documento de cada chiller no MongoDB."""
    logger.info("A inicializar memória de energia a partir do MongoDB...")
    for ip, dados in chillers_dict.items():
        last_doc = collection.find_one({"ip": ip}, sort=[('_id', -1)])
        if last_doc and "values" in last_doc and "medidor" in last_doc["values"]:
            kwh = last_doc["values"]["medidor"].get("EnergiaAtivaTotal_output")
            ts_str = last_doc.get("timestamp")
            if kwh is not None and ts_str:
                try:
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    memoria_energia[ip] = {
                        'last_kwh': float(kwh),
                        'last_time': ts.replace(minute=0, second=0, microsecond=0),
                    }
                    logger.info("  > %s: memória restaurada (last: %s kWh)", dados['nome'], kwh)
                except ValueError:
                    logger.warning("  > %s: timestamp inválido no último documento (%s)", dados['nome'], ts_str)


# --- Termodinâmica (CoolProp) ---

def calc_entalpia(temp_C, pressao_bar, fluido):
    if not _HAS_COOLPROP or temp_C is None or pressao_bar is None:
        return None
    try:
        T = temp_C + 273.15
        P_abs = (pressao_bar + 1.01325) * 1e5
        return PropsSI("H", "P", P_abs, "T", T, fluido) / 1000
    except Exception as e:
        logger.debug("calc_entalpia: %s", e)
        return None


def calc_estado_fluido(temp_C, pressao_bar, fluido):
    if not _HAS_COOLPROP or temp_C is None or pressao_bar is None:
        return "N/A"
    try:
        T = temp_C + 273.15
        P_abs = (pressao_bar + 1.01325) * 1e5
        h = PropsSI("H", "P", P_abs, "T", T, fluido)
        h_liq = PropsSI("H", "P", P_abs, "Q", 0, fluido)
        h_vap = PropsSI("H", "P", P_abs, "Q", 1, fluido)
        if abs(h - h_liq) < 0.1:
            return "Líquido Saturado"
        if abs(h - h_vap) < 0.1:
            return "Vapor Saturado"
        if h < h_liq:
            return "Líquido Sub-resfriado"
        if h > h_vap:
            return "Vapor Superaquecido"
        return "Mistura"
    except Exception as e:
        logger.debug("calc_estado_fluido: %s", e)
        return "Erro Calc"


def calculate_cop_ciclo(h1, h2, h3, h4, modo):
    """COP termodinâmico puro. Retorna None se qualquer valor for inválido."""
    if any(v is None for v in [h1, h2, h4]):
        return None
    trabalho = h2 - h1
    if trabalho <= 0:
        return None
    if modo == 'ARREFECIMENTO':
        if h3 is None:
            return None
        efeito = h4 - h3
        if efeito <= 0:
            return None
        cop = efeito / trabalho
    elif modo == 'AQUECIMENTO':
        efeito = h2 - h4
        if efeito <= 0:
            return None
        cop = efeito / trabalho
    else:
        return None
    return cop if 0 < cop <= 15 else None


def calcular_metricas_ciclo(temps, pressoes, modo, fluido):
    if not _HAS_COOLPROP:
        return {"rendimento": None, "aviso": "Sem CoolProp"}
    if not all(k in temps for k in ["T1", "T2", "T3", "T4"]) or not all(k in pressoes for k in ["P1", "P2"]):
        return {"rendimento": None, "aviso": "Sensores insuficientes"}

    h1 = h2 = h3 = h4 = None
    entalpias = {}
    cop_real = None
    aviso = ""
    try:
        h1 = calc_entalpia(temps["T1"], pressoes["P1"], fluido)
        h2 = calc_entalpia(temps["T2"], pressoes["P2"], fluido)

        if modo == 'ARREFECIMENTO':
            h3 = calc_entalpia(temps["T3"], pressoes["P2"], fluido)
            h4 = calc_entalpia(temps["T4"], pressoes["P1"], fluido)
        elif modo == 'AQUECIMENTO':
            h3 = calc_entalpia(temps["T3"], pressoes["P2"], fluido)
            h4 = calc_entalpia(temps["T4"], pressoes["P2"], fluido)

        entalpias = {"h1": h1, "h2": h2, "h3": h3, "h4": h4}
        cop_termo = calculate_cop_ciclo(h1, h2, h3, h4, modo)
        if cop_termo is not None:
            cop_real = cop_termo * (1.0 - IDADE_COMPRESSOR_FATOR)
            if not (0 < cop_real <= 15):
                cop_real = None
    except Exception as e:
        aviso = f"Erro cálculo: {e}"

    def _estado(t_key, p_bar):
        return calc_estado_fluido(temps.get(t_key), p_bar, fluido)

    p1 = pressoes.get("P1")
    p2 = pressoes.get("P2")
    estados = {
        "h1": _estado("T1", p1),
        "h2": _estado("T2", p2),
        "h3": _estado("T3", p2),
        "h4": _estado("T4", p1 if modo == 'ARREFECIMENTO' else p2),
    }
    return {"entalpias": entalpias, "estados": estados, "rendimento": cop_real, "aviso": aviso}


# --- Django ORM (substitui get_chillers_from_postgres / incrementar_horas_postgres) ---

def get_active_chillers():
    """Equivalente a get_chillers_from_postgres(), via Django ORM."""
    rows = Chiller.objects.exclude(status='desativado').values('ipcontrolador', 'nome', 'gas', 'ciclo')
    return {
        row['ipcontrolador']: {
            'nome': row['nome'],
            'gas': row['gas'] or DEFAULT_FLUIDO,
            'ciclo': row['ciclo'] or 'verao',
        }
        for row in rows
    }


def increment_hours(ip: str, horas: float):
    """Equivalente a incrementar_horas_postgres(), update atómico via F()/Coalesce()."""
    updated = Chiller.objects.filter(ipcontrolador=ip).update(
        horas_funcionamento=Coalesce(F('horas_funcionamento'), 0, output_field=DecimalField()) + horas
    )
    if not updated:
        logger.warning("increment_hours: nenhum chiller encontrado com ipcontrolador=%s", ip)


# --- Processamento principal (substitui process_chiller, sem I/O Modbus) ---

def process_chiller(ip, nome, timestamp, raw_data, collection, fluido_db, ciclo_db, received_at):
    """
    raw_data: dict já decodificado, no formato {"temps": {...}, "pressoes": {...}, "medidor": {...}}
    tal como devolvido por read_modbus_block() no script original — agora vem do payload MQTT.

    timestamp: string "%Y-%m-%d %H:%M:%S" tal como veio no payload — guardada tal-qual no
    documento Mongo, mesmo que seja considerada suspeita.
    received_at: datetime da receção da mensagem no servidor Django (capturado pelo listener
    MQTT), usado como fallback nos cálculos se o timestamp do payload for inválido/suspeito,
    e gravado no documento como "recebido_em" (ver nota abaixo).
    """
    fluido_real = fluido_db.strip().upper() if fluido_db else DEFAULT_FLUIDO
    ciclo_str = ciclo_db.lower() if ciclo_db else "verao"
    modo_operacao = MODO_MAP.get(ciclo_str, DEFAULT_MODO)

    timestamp_calculo = _resolver_timestamp_para_calculo(ip, timestamp, received_at)

    # "recebido_em" é a hora a que o servidor recebeu a mensagem, sempre fiável, ao contrário
    # de "timestamp" que vem do relógio do chiller e pode estar adiantado/atrasado. É por este
    # campo que se ordena para saber qual é a leitura mais recente (ver SensorDataAPIView):
    # ordenar pelo timestamp do payload deixaria uma leitura com relógio adiantado cravada no
    # topo, a passar por atual, até a hora real a alcançar.
    doc = {
        "timestamp": timestamp,
        "recebido_em": received_at.strftime("%Y-%m-%d %H:%M:%S"),
        "chiller": nome,
        "ip": ip,
        "fluido": fluido_real,
        "ciclo": ciclo_db,
        "values": raw_data,
    }

    medidor = doc["values"].setdefault("medidor", {})

    tensoes_vals = [medidor.get(k) for k in ("Tensao_L1_L2_output", "Tensao_L2_L3_output", "Tensao_L3_L1_output")]
    tensoes_vals = [v for v in tensoes_vals if v is not None and v > 0]
    if tensoes_vals:
        medidor["MediaTensoes_output"] = sum(tensoes_vals) / len(tensoes_vals)

    correntes_raw = [medidor.get("Corrente_L1_output"), medidor.get("Corrente_L2_output"), medidor.get("Corrente_L3_output")]
    correntes_validas = [c for c in correntes_raw if c is not None and c >= 1.0]
    avg_corrente = sum(correntes_validas) / len(correntes_validas) if correntes_validas else 0.0
    medidor["MediaCorrentes_output"] = avg_corrente

    if avg_corrente <= LIMIAR_CORRENTE_ATIVO:
        medidor["estado_chiller"] = "standby"
        _registar_leitura_e_delta_horas(ip, timestamp_calculo)  # só atualiza o relógio; standby não soma horímetro
        kwh_standby = medidor.get("EnergiaAtivaTotal_output")
        calculate_energy_cost(ip, kwh_standby, timestamp_calculo)
        logger.info("[%s] Standby. Corrente: %.2f A", nome, avg_corrente)
        _insert_doc(collection, doc, nome)
        return doc

    medidor["estado_chiller"] = "ativo"

    delta_horas = _registar_leitura_e_delta_horas(ip, timestamp_calculo)
    if delta_horas is not None:
        increment_hours(ip, delta_horas)

    if _HAS_COOLPROP:
        res_ciclo = calcular_metricas_ciclo(doc["values"].get("temps", {}), doc["values"].get("pressoes", {}), modo_operacao, fluido_real)
        doc["values"].update(res_ciclo)
        if res_ciclo["rendimento"] is not None:
            logger.info("[%s] COP Real: %.2f", nome, res_ciclo["rendimento"])

    kwh_total = medidor.get("EnergiaAtivaTotal_output")
    delta, custo = calculate_energy_cost(ip, kwh_total, timestamp_calculo)
    medidor["Energia_consumida_hora"] = delta if delta else 0.0
    medidor["custo_hora"] = custo if custo else 0.0

    if custo and custo > 0:
        logger.info("[%s] Custo: %.3f€", nome, custo)

    logger.info("[%s] Ativo. Corrente: %.2f A", nome, avg_corrente)
    _insert_doc(collection, doc, nome)
    return doc


def _insert_doc(collection, doc, nome):
    try:
        collection.insert_one(doc)
        logger.info("%s: dados guardados no MongoDB.", nome)
    except Exception as e:
        logger.error("%s: erro ao gravar no MongoDB: %s", nome, e)
