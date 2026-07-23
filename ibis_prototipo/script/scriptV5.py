import time
import math
import logging
from datetime import datetime
import warnings
import psycopg2
from pymongo import MongoClient, DESCENDING
from pymodbus.client import ModbusTcpClient
from pymodbus.payload import BinaryPayloadDecoder
from pymodbus.constants import Endian

try:
    from CoolProp.CoolProp import PropsSI
    _HAS_COOLPROP = True
except ImportError:
    _HAS_COOLPROP = False
    print("AVISO: CoolProp não instalado. O cálculo de COP/Entalpias será ignorado.")

# --- 1. CONFIGURAÇÃO ---
warnings.filterwarnings("ignore", category=DeprecationWarning)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger()

# --- 2. CONSTANTES ---
DEFAULT_FLUIDO = "R407C"
DEFAULT_MODO = "ARREFECIMENTO"
LIMIAR_CORRENTE_ATIVO = 2.0   
PRESSURE_DIVIDER = 14.5038    
IDADE_COMPRESSOR_FATOR = 0.07 

memoria_energia = {}

PRECO_TARIFAS = {'ponta': 0.05136, 'cheias': 0.06072, 'vazio': 0.05073, 'super_vazio': 0.05101}
PERIODOS_TARIFARIOS = {
    'verao': {
        'weekday': [(0.0, 0.5, 'cheias'), (0.5, 2.0, 'vazio'), (2.0, 6.0, 'super_vazio'), (6.0, 7.5, 'vazio'), (7.5, 14.0, 'cheias'), (14.0, 17.0, 'ponta'), (17.0, 24.0, 'cheias')],
        'saturday': [(0.0, 3.5, 'vazio'), (3.5, 7.0, 'super_vazio'), (7.0, 10.0, 'vazio'), (10.0, 13.5, 'cheias'), (13.5, 19.5, 'vazio'), (19.5, 23.0, 'cheias'), (23.0, 24.0, 'vazio')],
        'sunday': [(0.0, 4.0, 'vazio'), (4.0, 8.0, 'super_vazio'), (8.0, 24.0, 'vazio'),]
    },
    'inverno': {
        'weekday': [(0.0, 0.5, 'cheias'), (0.5, 2.0, 'vazio'), (2.0, 6.0, 'super_vazio'), (6.0, 7.5, 'vazio'), (7.5, 17.0, 'cheias'), (17.0, 22.0, 'ponta'), (22.0, 24.0, 'cheias'),],
        'saturday': [(0.0, 3.0, 'vazio'), (3.0, 7.0, 'super_vazio'), (7.0, 10.5, 'vazio'), (10.5, 12.5, 'cheias'), (12.5, 17.3, 'vazio'), (17.3, 22.5, 'cheias'), (22.5, 24.0, 'vazio'),],
        'sunday': [(0.0, 4.0, 'vazio'), (4.0, 8.0, 'super_vazio'), (8.0, 24.0, 'vazio'),]
    }
}

# --- 3. MAPA DE ENDEREÇOS MODBUS ---
variables = {
    # 16-bit (Temperaturas e Pressões)
    8960: ("T1_output", "temp", "ºC"), 
    8961: ("T2_output", "temp", "ºC"), 
    8962: ("T3_output", "temp", "ºC"), 
    8963: ("T4_output", "temp", "ºC"), 
    8964: ("T5_output", "temp", "ºC"), 
    8965: ("P1_output", "pressao", "bar"), 
    8966: ("P2_output", "pressao", "bar"), 
    
    # 32-bit Float (Medidor Elétrico)
    8973: ("Corrente_L1_output", "float32", "A"), 
    8975: ("Corrente_L2_output", "float32", "A"), 
    8977: ("Corrente_L3_output", "float32", "A"), 
    
    8985: ("Tensao_L1_L2_output", "float32", "V"), 
    8987: ("Tensao_L2_L3_output", "float32", "V"), 
    8989: ("Tensao_L3_L1_output", "float32", "V"), 
    
    8993: ("EnergiaAtivaParcial_output", "float32", "kWh"), 
    8997: ("EnergiaReativaParcial_output", "float32", "kVArh"), 
    9001: ("EnergiaAtivaTotal_output", "float32", "kWh"), 
    9005: ("EnergiaReativaTotal_output", "float32", "kVArh"),
    
    9017: ("PotenciaAtivaTotal_kW", "float32", "kW"),
}

# Endereços que precisam de inversão de WORD (Little Endian Word, Big Endian Byte)
ENDERECOS_LITTLE_BIG = { 
    8973, 8975, 8977, 8985, 8987, 8989, 
    8993, 8997, 9001, 9005,
    9017
} 

_SENSORES_ESPERADOS_TEMP = ["T1", "T2", "T3", "T4", "T5"]
_SENSORES_ESPERADOS_PRES = ["P1", "P2"]
_MEDIDOR_ESPERADO = [
    "Tensao_L1_L2_output", "Tensao_L2_L3_output", "Tensao_L3_L1_output",
    "Corrente_L1_output", "Corrente_L2_output", "Corrente_L3_output",
]

def salvar_txt(doc):
    try:
        vals = doc['values']
        temps    = vals.get('temps', {})
        pressoes = vals.get('pressoes', {})
        med      = vals.get('medidor', {})

        with open("leituras_output.txt", "a", encoding="utf-8") as f:
            f.write(f"--- Leitura: {doc['timestamp']} ---\n")
            f.write(f"Chiller: {doc['chiller']} ({doc['ip']})\n")

            # Sensores — marca falha individual
            f.write(" [Sensores]\n")
            for k in _SENSORES_ESPERADOS_TEMP:
                if k in temps:
                    f.write(f"   {k}: {temps[k]:.1f} ºC\n")
                else:
                    f.write(f"   {k}: Falha na leitura\n")
            for k in _SENSORES_ESPERADOS_PRES:
                if k in pressoes:
                    f.write(f"   {k}: {pressoes[k]:.2f} bar\n")
                else:
                    f.write(f"   {k}: Falha na leitura\n")

            # Elétrico
            f.write(" [Eletrico]\n")
            ordem_med = [
                "Tensao_L1_L2_output", "Tensao_L2_L3_output", "Tensao_L3_L1_output",
                "MediaTensoes_output",
                "Corrente_L1_output", "Corrente_L2_output", "Corrente_L3_output",
                "MediaCorrentes_output", "PotenciaAtivaTotal_kW",
                "EnergiaAtivaParcial_output", "EnergiaReativaParcial_output",
                "EnergiaAtivaTotal_output", "EnergiaReativaTotal_output",
            ]
            for k in ordem_med:
                if k in _MEDIDOR_ESPERADO and k not in med:
                    f.write(f"   {k}: Falha na leitura\n")
                    continue
                if k not in med:
                    continue
                if "Corrente" in k: unit = "A"
                elif "Tensao" in k or k == "MediaTensoes_output": unit = "V"
                elif "Potencia" in k: unit = "kW"
                elif "Energia" in k: unit = "kVArh" if "Reativa" in k else "kWh"
                else: unit = ""
                f.write(f"   {k}: {med[k]:.3f} {unit}\n")

            # Entalpias com estado do fluido
            entalpias = vals.get('entalpias', {})
            estados   = vals.get('estados', {})
            nomes_h = {
                "h1": "Entrada Compressor",
                "h2": "Saída Compressor",
                "h3": "Saída Condensador",
                "h4": "Saída Evaporador" if doc.get('ciclo', 'verao') != 'inverno' else "Saída Condensador (ref)",
            }
            if entalpias:
                f.write(" [Entalpias]\n")
                for hk, label in nomes_h.items():
                    hval  = entalpias.get(hk)
                    estado = estados.get(hk, "N/A")
                    if hval is not None:
                        f.write(f"   {hk} ({label}): {hval:.3f} kJ/kg  [{estado}]\n")
                    else:
                        f.write(f"   {hk} ({label}): Não calculado\n")

            # Performance
            estado_chiller = med.get('estado_chiller', 'N/A')
            rendimento     = vals.get('rendimento')
            aviso_cop      = vals.get('aviso', '')
            consumo        = med.get('Energia_consumida_hora')
            custo          = med.get('custo_hora')

            f.write(" [Performance]\n")
            f.write(f"   Estado: {estado_chiller}\n")
            if rendimento is not None:
                f.write(f"   COP Real: {rendimento:.2f}\n")
            elif estado_chiller == 'ativo':
                motivo = aviso_cop if aviso_cop else "dados de pressão/temperatura insuficientes"
                f.write(f"   COP: Cálculo Impossível ({motivo})\n")
            if consumo is not None:
                f.write(f"   Consumo hora: {consumo:.3f} kWh\n")
            if custo is not None:
                f.write(f"   Custo hora:   {custo:.4f} €\n")

            f.write("\n")
            f.flush()
    except Exception as e:
        print(f"Erro TXT: {e}")

# --- 4. FUNÇÕES DE BASE DE DADOS ---
def get_chillers_from_postgres():
    try:
        conn = psycopg2.connect(dbname="orlis_local", user="postgres", password="postgres", host="localhost", port="5432")
        cur = conn.cursor()
        cur.execute("SELECT ipcontrolador, nome, gas, ciclo FROM chiller WHERE status != 'desativado';")
        rows = cur.fetchall()
        chillers = {}
        for row in rows:
            ip, nome, gas, ciclo = row
            chillers[ip] = {
                "nome": nome,
                "gas": gas if gas else DEFAULT_FLUIDO,
                "ciclo": ciclo if ciclo else "verao"
            }
        conn.close()
        return chillers
    except Exception as e:
        logger.error(f"Postgres Error: {e}")
        return {}

def setup_mongodb():
    try:
        client = MongoClient("mongodb://localhost:27017")
        db = client["orlis_db"]
        return client, db["values"]
    except Exception as e:
        logger.error(f"MongoDB Error: {e}")
        return None, None

def incrementar_horas_postgres(ip, minutos_decorridos):
    try:
        conn = psycopg2.connect(dbname="orlis_local", user="postgres", password="postgres", host="localhost", port="5432")
        cur = conn.cursor()
        horas_a_somar = minutos_decorridos / 60.0
        sql = "UPDATE chiller SET horas_funcionamento = COALESCE(horas_funcionamento, 0) + %s WHERE ipcontrolador = %s;"
        cur.execute(sql, (horas_a_somar, ip))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"  > [ERRO PG] Falha ao atualizar horas: {e}")

def init_energy_memory(collection, chillers_dict):
    global memoria_energia
    print("A inicializar memória de energia a partir do MongoDB...")
    for ip, dados in chillers_dict.items():
        last_doc = collection.find_one({"ip": ip}, sort=[('_id', DESCENDING)])
        if last_doc and "values" in last_doc and "medidor" in last_doc["values"]:
            kwh = last_doc["values"]["medidor"].get("EnergiaAtivaTotal_output")
            ts_str = last_doc.get("timestamp")
            if kwh is not None and ts_str:
                try:
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    memoria_energia[ip] = {
                        'last_kwh': float(kwh),
                        'last_time': ts.replace(minute=0, second=0, microsecond=0)
                    }
                    print(f"  > {dados['nome']}: Memória restaurada (Last: {kwh} kWh)")
                except Exception:
                    pass

# --- 5. FUNÇÕES DE CÁLCULO ---
def _is_horario_verao(dt: datetime) -> bool:
    ano = dt.year
    inicio = max(datetime(ano, 3, d) for d in range(25, 32) if datetime(ano, 3, d).weekday() == 6)
    fim    = max(datetime(ano, 10, d) for d in range(25, 32) if datetime(ano, 10, d).weekday() == 6)
    return inicio.date() <= dt.date() < fim.date()

def get_tarifa(dt: datetime):
    dia_semana = dt.weekday()
    hora = dt.hour + dt.minute / 60
    periodo = 'verao' if _is_horario_verao(dt) else 'inverno'
    if dia_semana < 5: periods = PERIODOS_TARIFARIOS[periodo]['weekday']
    elif dia_semana == 5: periods = PERIODOS_TARIFARIOS[periodo]['saturday']
    else: periods = PERIODOS_TARIFARIOS[periodo]['sunday']
    for start, end, ciclo_nome in periods:
        if start <= hora < end: return PRECO_TARIFAS[ciclo_nome]
    return PRECO_TARIFAS['vazio']

def calculate_energy_cost(ip: str, energia_total: float, timestamp_str: str):
    global memoria_energia
    if energia_total is None or energia_total < 0: return None, None

    agora = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
    hora_cheia_atual = agora.replace(minute=0, second=0, microsecond=0)

    if ip not in memoria_energia:
        memoria_energia[ip] = {'last_kwh': energia_total, 'last_time': hora_cheia_atual}
        return None, None

    dados_antigos = memoria_energia[ip]

    if hora_cheia_atual > dados_antigos['last_time']:
        # Nova hora — reseta baseline sem cobrar (próxima leitura já acumula a partir daqui)
        memoria_energia[ip] = {'last_kwh': energia_total, 'last_time': hora_cheia_atual}
        return 0.0, 0.0

    # Mesma hora — acumula delta desde o início da hora atual
    delta_kwh = energia_total - dados_antigos['last_kwh']
    if delta_kwh < 0:
        logger.warning(f"[{ip}] Contador resetou ({dados_antigos['last_kwh']:.1f} → {energia_total:.1f} kWh). Baseline actualizada.")
        memoria_energia[ip] = {'last_kwh': energia_total, 'last_time': hora_cheia_atual}
        return 0.0, 0.0
    if delta_kwh >= 800:
        logger.warning(f"[{ip}] Delta kWh anómalo ({delta_kwh:.1f} kWh). Ignorado.")
        return 0.0, 0.0

    tarifa = get_tarifa(agora)
    return delta_kwh, delta_kwh * tarifa

def calc_entalpia(temp_C, pressao_bar, fluido):
    if not _HAS_COOLPROP or temp_C is None or pressao_bar is None: return None
    try:
        T = temp_C + 273.15
        P_abs = (pressao_bar + 1.01325) * 1e5
        return PropsSI("H", "P", P_abs, "T", T, fluido) / 1000
    except Exception as e:
        logger.debug(f"calc_entalpia: {e}")
        return None

def calc_estado_fluido(temp_C, pressao_bar, fluido):
    if not _HAS_COOLPROP or temp_C is None or pressao_bar is None: return "N/A"
    try:
        T = temp_C + 273.15
        P_abs = (pressao_bar + 1.01325) * 1e5
        h = PropsSI("H", "P", P_abs, "T", T, fluido)
        h_liq = PropsSI("H", "P", P_abs, "Q", 0, fluido)
        h_vap = PropsSI("H", "P", P_abs, "Q", 1, fluido)
        if abs(h - h_liq) < 0.1: return "Líquido Saturado"
        if abs(h - h_vap) < 0.1: return "Vapor Saturado"
        if h < h_liq: return "Líquido Sub-resfriado"
        if h > h_vap: return "Vapor Superaquecido"
        return "Mistura"
    except Exception as e:
        logger.debug(f"calc_estado_fluido: {e}")
        return "Erro Calc"

def calculate_cop_ciclo(h1, h2, h3, h4, modo):
    """COP termodinâmico puro. Retorna None se qualquer valor for inválido."""
    if any(v is None for v in [h1, h2, h4]):
        return None
    trabalho = h2 - h1
    if trabalho <= 0:
        return None
    if modo == 'ARREFECIMENTO':
        if h3 is None: return None
        efeito = h4 - h3
        if efeito <= 0: return None
        cop = efeito / trabalho
    elif modo == 'AQUECIMENTO':
        efeito = h2 - h4
        if efeito <= 0: return None
        cop = efeito / trabalho
    else:
        return None
    return cop if 0 < cop <= 15 else None

def calcular_metricas_ciclo(temps, pressoes, modo, fluido):
    if not _HAS_COOLPROP: return {"rendimento": None, "aviso": "Sem CoolProp"}
    if not all(k in temps for k in ["T1","T2","T3","T4"]) or not all(k in pressoes for k in ["P1","P2"]):
        return {"rendimento": None, "aviso": "Sensores insuficientes"}
        
    h1 = h2 = h3 = h4 = None
    entalpias = {}
    cop_real = None
    aviso = ""
    try:
        h1 = calc_entalpia(temps["T1"], pressoes["P1"], fluido)
        h2 = calc_entalpia(temps["T2"], pressoes["P2"], fluido)

        if modo == 'ARREFECIMENTO':
            h3 = calc_entalpia(temps["T3"], pressoes["P2"], fluido)  # condensador → alta pressão
            h4 = calc_entalpia(temps["T4"], pressoes["P1"], fluido)  # evaporador → baixa pressão
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
        aviso = f"Erro cálculo: {str(e)}"

    # Calcular estado do fluido para cada ponto do ciclo
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

# --- 6. LEITURA MODBUS ---
def read_modbus_block(client):
    """Lê todos os dados de uma vez."""
    try:
        start = 8960
        count = (9017 - 8960) + 2
        rr = client.read_holding_registers(address=start - 1, count=count)
        
        if rr.isError(): return None
        regs = rr.registers
        data = {"temps": {}, "pressoes": {}, "medidor": {}}
        
        for addr, (name, tipo, unit) in variables.items():
            idx = addr - start
            val = None
            if tipo == "float32":
                if idx + 1 < len(regs):
                    # CORREÇÃO DE CONTEXTO: Byte Order BIG, Word Order LITTLE se na lista
                    word_order = Endian.LITTLE if addr in ENDERECOS_LITTLE_BIG else Endian.BIG
                    byte_order = Endian.BIG
                    decoder = BinaryPayloadDecoder.fromRegisters(regs[idx:idx+2], byteorder=byte_order, wordorder=word_order)
                    val = decoder.decode_32bit_float()
            elif tipo in ["temp", "pressao"]:
                raw = regs[idx]
                if tipo == "temp":
                    if raw >= 0x8000: raw -= 0x10000 
                    val = raw / 10.0
                elif tipo == "pressao":
                    val = raw / PRESSURE_DIVIDER 
            
            if val is not None:
                if not math.isfinite(val): continue
                if tipo == "pressao" and (val < -1 or val > 60): continue
                if tipo == "temp" and (val < -50 or val > 150): continue
                
                if tipo == "temp": data["temps"][name.split('_')[0]] = val
                elif tipo == "pressao": data["pressoes"][name.split('_')[0]] = val
                else: data["medidor"][name] = val
        return data
    except Exception as e:
        logger.error(f"Erro decode Modbus: {e}")
        return None

# --- 7. PROCESSAMENTO PRINCIPAL ---
def process_chiller(client, ip, nome, collection, fluido_db, ciclo_db):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    fluido_real = fluido_db.strip().upper() if fluido_db else DEFAULT_FLUIDO
    modo_map = {'verao': 'ARREFECIMENTO', 'frio': 'ARREFECIMENTO', 'inverno': 'AQUECIMENTO', 'quente': 'AQUECIMENTO'}
    ciclo_str = ciclo_db.lower() if ciclo_db else "verao"
    modo_operacao = modo_map.get(ciclo_str, DEFAULT_MODO)

    # 1. Leitura
    raw_data = read_modbus_block(client)
    if raw_data is None: 
        logger.warning(f"{nome}: Falha leitura Modbus")
        return

    doc = {
        "timestamp": timestamp,
        "chiller": nome,
        "ip": ip,
        "fluido": fluido_real,
        "ciclo": ciclo_db,
        "values": raw_data
    }

    # 2. Dados Elétricos
    medidor = doc["values"]["medidor"]

    # Média de tensões linha-linha
    tensoes_vals = [medidor.get(k) for k in ("Tensao_L1_L2_output", "Tensao_L2_L3_output", "Tensao_L3_L1_output")]
    tensoes_vals = [v for v in tensoes_vals if v is not None and v > 0]
    if tensoes_vals:
        medidor["MediaTensoes_output"] = sum(tensoes_vals) / len(tensoes_vals)

    # Média de correntes — só fases com leitura plausível para não diluir por fases com falha de sensor
    correntes_raw = [medidor.get("Corrente_L1_output"), medidor.get("Corrente_L2_output"), medidor.get("Corrente_L3_output")]
    correntes_validas = [c for c in correntes_raw if c is not None and c >= 1.0]
    avg_corrente = sum(correntes_validas) / len(correntes_validas) if correntes_validas else 0.0
    medidor["MediaCorrentes_output"] = avg_corrente

    if avg_corrente <= LIMIAR_CORRENTE_ATIVO:
        medidor["estado_chiller"] = "standby"
        # Mantém memória de energia atualizada para evitar pico falso na próxima sessão ativa
        kwh_standby = medidor.get("EnergiaAtivaTotal_output")
        calculate_energy_cost(ip, kwh_standby, timestamp)
        salvar_txt(doc)
        print(f"\n[{nome}] Standby. Corrente: {avg_corrente:.2f} A")
        if collection is not None:
            try:
                collection.insert_one(doc)
            except Exception as e:
                logger.error(f"Erro Mongo Insert (standby): {e}")
        return

    medidor["estado_chiller"] = "ativo"

    # 3. Atualiza horímetro no Postgres
    incrementar_horas_postgres(ip, 1.0)

    # 4. Calcula Termodinâmica
    if _HAS_COOLPROP:
        res_ciclo = calcular_metricas_ciclo(doc["values"]["temps"], doc["values"]["pressoes"], modo_operacao, fluido_real)
        doc["values"].update(res_ciclo)
        if res_ciclo["rendimento"] is not None:
            print(f"  > COP Real: {res_ciclo['rendimento']:.2f}")

    # 5. Calcula Custos
    kwh_total = medidor.get("EnergiaAtivaTotal_output")
    delta, custo = calculate_energy_cost(ip, kwh_total, timestamp)
    medidor["Energia_consumida_hora"] = delta if delta else 0.0
    medidor["custo_hora"] = custo if custo else 0.0

    if custo and custo > 0:
        print(f"  > CUSTO: {custo:.3f}€")

    # 6. Guarda TXT e MongoDB (após todos os cálculos — COP e custos já estão no doc)
    salvar_txt(doc)
    print(f"\n[{nome}] TXT Atualizado. Corrente: {avg_corrente:.2f} A")
    if collection is not None:
        try:
            collection.insert_one(doc)
            logger.info(f"{nome}: Dados guardados na BD.")
        except Exception as e:
            logger.error(f"Erro Mongo Insert: {e}")

# --- 8. LOOP PRINCIPAL ---
def main():
    print("--- MONITORIZAÇÃO CHILLER V5.2 ---")

    # Cabeçalho de sessão no ficheiro de log
    with open("leituras_output.txt", "a", encoding="utf-8") as f:
        sep = "=" * 52
        f.write(f"\n{sep}\n")
        f.write(f"INÍCIO DA SESSÃO: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"{sep}\n\n")
        f.flush()

    controladores = get_chillers_from_postgres()
    if not controladores:
        print("AVISO: Nenhum chiller encontrado na BD. A usar fallback.")
        controladores = {"10.99.120.29": {"nome": "Chiller Demo", "gas": "R407C", "ciclo": "verao"}}

    mongo_client, collection = setup_mongodb()
    if collection is not None:
        init_energy_memory(collection, controladores)

    INTERVALO_S = 60
    try:
        while True:
            t_inicio = time.monotonic()

            for ip, dados in controladores.items():
                nome = dados["nome"]
                fluido = dados["gas"]
                ciclo = dados["ciclo"]

                try:
                    client = ModbusTcpClient(ip, port=502, timeout=5)
                    if client.connect():
                        try:
                            process_chiller(client, ip, nome, collection, fluido, ciclo)
                        finally:
                            client.close()
                    else:
                        logger.warning(f"{nome} ({ip}): Sem resposta")
                except Exception as e:
                    logger.error(f"Erro loop {nome}: {e}")

                time.sleep(1)

            decorrido = time.monotonic() - t_inicio
            espera = max(0.0, INTERVALO_S - decorrido)
            print(f"--- Ciclo em {decorrido:.1f}s. Aguardando {espera:.1f}s ---")
            time.sleep(espera)

    except KeyboardInterrupt:
        print("\nA parar script...")
        if mongo_client: mongo_client.close()

if __name__ == "__main__":
    main()