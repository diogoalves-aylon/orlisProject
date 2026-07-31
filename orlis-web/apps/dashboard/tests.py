"""
Testes do processamento de telemetria (apps.dashboard.services.telemetry) e da receção
das mensagens MQTT (apps.dashboard.management.commands.mqtt_listener).

O módulo é quase todo puro, o que o torna barato de testar sem broker nem hardware.
As exceções são increment_hours() e get_active_chillers(), que tocam no Postgres, e
process_chiller(), que grava no MongoDB — para esse último basta uma coleção falsa,
porque só se lhe chama insert_one().

Atenção ao estado global: memoria_energia e ultima_leitura_por_chiller vivem no módulo
e sobrevivem entre testes. Todos os casos abaixo limpam-nos no setUp.
"""
import json
from datetime import datetime
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.messages.storage.cookie import CookieStorage
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.test import RequestFactory, SimpleTestCase, TestCase

from apps.cliente.models import ClienteProfile
# A área de cliente tem cópias com o mesmo nome das views do dashboard; renomeadas aqui
# para as duas poderem ser testadas no mesmo módulo.
from apps.cliente.views import (
    ClienteLogoView as ClienteLogoAreaView, RelatorioView as ClienteRelatorioAreaView,
    VariableLogView as ClienteVariableLogAreaView,
)
from apps.dashboard.management.commands import mqtt_listener
from apps.dashboard.models import Chiller, Cliente, Intervencao
from apps.dashboard.services import telemetry
from apps.dashboard.views import (
    ClienteChillerDetailView, ClienteDeleteView, ClienteLogoView, ClienteUpdateView,
    DashboardView, LivroObraView, RegistarIntervencaoView, RelatorioView,
    SensorDataAPIView, TableClientsView, VariableLogView as DashboardVariableLogView,
)


class ColecaoFalsa:
    """Faz de coleção do MongoDB. process_chiller() só precisa de insert_one()."""

    def __init__(self):
        self.documentos = []

    def insert_one(self, doc):
        self.documentos.append(doc)


class LimpaEstadoGlobalMixin:
    def setUp(self):
        super().setUp()
        telemetry.memoria_energia.clear()
        telemetry.ultima_leitura_por_chiller.clear()


# --- Tarifário -------------------------------------------------------------------

class TarifarioTests(SimpleTestCase):
    def test_ponta_em_dia_util_de_verao(self):
        # 2026-07-27 é uma segunda-feira, dentro do horário de verão.
        # No período de verão a ponta em dia útil vai das 14h às 17h.
        self.assertTrue(telemetry._is_horario_verao(datetime(2026, 7, 27, 15, 0)))
        self.assertEqual(
            telemetry.get_tarifa(datetime(2026, 7, 27, 15, 0)),
            telemetry.PRECO_TARIFAS['ponta'],
        )

    def test_super_vazio_de_madrugada(self):
        self.assertEqual(
            telemetry.get_tarifa(datetime(2026, 7, 27, 4, 0)),
            telemetry.PRECO_TARIFAS['super_vazio'],
        )

    def test_domingo_nao_tem_ponta(self):
        # 2026-07-26 é domingo: o perfil de domingo só tem vazio e super vazio.
        domingo = datetime(2026, 7, 26, 15, 0)
        self.assertEqual(domingo.weekday(), 6)
        self.assertNotEqual(telemetry.get_tarifa(domingo), telemetry.PRECO_TARIFAS['ponta'])

    def test_inverno_desloca_a_ponta_para_a_noite(self):
        # Em janeiro a ponta em dia útil é das 17h às 22h, não das 14h às 17h.
        self.assertFalse(telemetry._is_horario_verao(datetime(2026, 1, 5, 18, 0)))
        self.assertEqual(
            telemetry.get_tarifa(datetime(2026, 1, 5, 18, 0)),
            telemetry.PRECO_TARIFAS['ponta'],
        )


# --- Custo de energia ------------------------------------------------------------

class CustoEnergiaTests(LimpaEstadoGlobalMixin, SimpleTestCase):
    IP = "10.0.0.1"

    def test_primeira_leitura_so_estabelece_a_baseline(self):
        delta, custo = telemetry.calculate_energy_cost(self.IP, 1000.0, "2026-07-27 15:10:00")
        self.assertIsNone(delta)
        self.assertIsNone(custo)
        self.assertEqual(telemetry.memoria_energia[self.IP]['last_kwh'], 1000.0)

    def test_consumo_dentro_da_mesma_hora_e_cobrado(self):
        telemetry.calculate_energy_cost(self.IP, 1000.0, "2026-07-27 15:10:00")
        delta, custo = telemetry.calculate_energy_cost(self.IP, 1002.5, "2026-07-27 15:40:00")
        self.assertAlmostEqual(delta, 2.5)
        self.assertAlmostEqual(custo, 2.5 * telemetry.PRECO_TARIFAS['ponta'])

    def test_mudanca_de_hora_reinicia_a_contagem(self):
        telemetry.calculate_energy_cost(self.IP, 1000.0, "2026-07-27 15:10:00")
        delta, custo = telemetry.calculate_energy_cost(self.IP, 1005.0, "2026-07-27 16:05:00")
        self.assertEqual((delta, custo), (0.0, 0.0))
        self.assertEqual(telemetry.memoria_energia[self.IP]['last_kwh'], 1005.0)

    def test_contador_que_reseta_nao_gera_custo_negativo(self):
        telemetry.calculate_energy_cost(self.IP, 1000.0, "2026-07-27 15:10:00")
        delta, custo = telemetry.calculate_energy_cost(self.IP, 5.0, "2026-07-27 15:20:00")
        self.assertEqual((delta, custo), (0.0, 0.0))
        # A baseline acompanha o contador novo, senão a leitura seguinte dava um salto enorme.
        self.assertEqual(telemetry.memoria_energia[self.IP]['last_kwh'], 5.0)

    def test_delta_anomalo_e_ignorado(self):
        telemetry.calculate_energy_cost(self.IP, 1000.0, "2026-07-27 15:10:00")
        delta, custo = telemetry.calculate_energy_cost(self.IP, 1900.0, "2026-07-27 15:20:00")
        self.assertEqual((delta, custo), (0.0, 0.0))
        # Ao contrário do reset, aqui a baseline NÃO se move: a leitura é tida por
        # inválida, não por um contador novo.
        self.assertEqual(telemetry.memoria_energia[self.IP]['last_kwh'], 1000.0)

    def test_energia_invalida_nao_e_contabilizada(self):
        self.assertEqual(telemetry.calculate_energy_cost(self.IP, None, "2026-07-27 15:10:00"), (None, None))
        self.assertEqual(telemetry.calculate_energy_cost(self.IP, -5.0, "2026-07-27 15:10:00"), (None, None))


# --- Horímetro -------------------------------------------------------------------

class HorimetroTests(LimpaEstadoGlobalMixin, SimpleTestCase):
    IP = "10.0.0.2"

    def test_primeira_leitura_nao_soma(self):
        self.assertIsNone(telemetry._registar_leitura_e_delta_horas(self.IP, "2026-07-27 15:00:00"))

    def test_soma_o_intervalo_real_entre_leituras(self):
        telemetry._registar_leitura_e_delta_horas(self.IP, "2026-07-27 15:00:00")
        delta = telemetry._registar_leitura_e_delta_horas(self.IP, "2026-07-27 15:30:00")
        self.assertAlmostEqual(delta, 0.5)

    def test_relogio_que_nao_avanca_vira_nova_baseline(self):
        telemetry._registar_leitura_e_delta_horas(self.IP, "2026-07-27 15:00:00")
        self.assertIsNone(telemetry._registar_leitura_e_delta_horas(self.IP, "2026-07-27 14:00:00"))
        # A baseline passou a ser a leitura recuada, para a próxima contar a partir dela.
        self.assertEqual(
            telemetry.ultima_leitura_por_chiller[self.IP], datetime(2026, 7, 27, 14, 0)
        )

    def test_gap_acima_do_limite_nao_conta_como_funcionamento(self):
        telemetry._registar_leitura_e_delta_horas(self.IP, "2026-07-27 10:00:00")
        hora_seguinte = 10 + int(telemetry.MAX_GAP_HORAS) + 1
        self.assertIsNone(
            telemetry._registar_leitura_e_delta_horas(self.IP, f"2026-07-27 {hora_seguinte:02d}:00:00")
        )

    def test_gap_no_limite_ainda_conta(self):
        telemetry._registar_leitura_e_delta_horas(self.IP, "2026-07-27 10:00:00")
        delta = telemetry._registar_leitura_e_delta_horas(self.IP, "2026-07-27 12:00:00")
        self.assertAlmostEqual(delta, telemetry.MAX_GAP_HORAS)


# --- Validação do timestamp do payload -------------------------------------------

class TimestampDoPayloadTests(SimpleTestCase):
    IP = "10.0.0.3"

    def test_timestamp_plausivel_e_aceite(self):
        recebido = datetime(2026, 7, 27, 15, 0, 0)
        self.assertEqual(
            telemetry._resolver_timestamp_para_calculo(self.IP, "2026-07-27 15:00:05", recebido),
            "2026-07-27 15:00:05",
        )

    def test_timestamp_malformado_cai_na_hora_de_rececao(self):
        recebido = datetime(2026, 7, 27, 15, 0, 0)
        self.assertEqual(
            telemetry._resolver_timestamp_para_calculo(self.IP, "ontem à tarde", recebido),
            "2026-07-27 15:00:00",
        )

    def test_relogio_muito_adiantado_cai_na_hora_de_rececao(self):
        recebido = datetime(2026, 7, 27, 15, 0, 0)
        self.assertEqual(
            telemetry._resolver_timestamp_para_calculo(self.IP, "2026-07-27 18:00:00", recebido),
            "2026-07-27 15:00:00",
        )

    def test_relogio_muito_atrasado_tambem_cai(self):
        recebido = datetime(2026, 7, 27, 15, 0, 0)
        self.assertEqual(
            telemetry._resolver_timestamp_para_calculo(self.IP, "2026-07-27 12:00:00", recebido),
            "2026-07-27 15:00:00",
        )


# --- Forma do payload MQTT -------------------------------------------------------

# Mensagem copiada tal-qual do broker de produção (172.20.50.162:8883, tópico
# iot/raspberry-aylon/chillers/chiller_1/telemetry, 31/07/2026). As leituras vêm dentro
# de "values" — com a validação antiga, que as procurava no topo do payload, TODAS as
# mensagens reais eram descartadas e nada chegava ao MongoDB.
PAYLOAD_RASPBERRY = {
    "timestamp": "2026-07-31 10:48:45",
    "chiller": "Chiller 1",
    "ip": "10.0.0.1",
    "fluido": "R407C",
    "ciclo": "verao",
    "values": {
        "temps": {"T1": 16.3, "T2": 71.0, "T3": 38.0, "T4": 39.3, "T5": 10.7},
        "pressoes": {"P1": 17.167914615480083, "P2": 19.374232959638164},
        "medidor": {
            "Corrente_L1_output": 0.0, "Corrente_L2_output": 0.0, "Corrente_L3_output": 0.0,
            "Tensao_L1_L2_output": 418.91082763671875, "Tensao_L2_L3_output": 419.19793701171875,
            "Tensao_L3_L1_output": 418.90997314453125,
            "EnergiaAtivaParcial_output": 15874.462890625, "EnergiaReativaParcial_output": 16288.80859375,
            "EnergiaAtivaTotal_output": 15874.462890625, "EnergiaReativaTotal_output": 16288.80859375,
            "PotenciaAtivaTotal_kW": 0.0, "MediaTensoes_output": 419.00624593098956,
            "MediaCorrentes_output": 0.0, "estado_chiller": "standby",
        },
    },
}

# Contrato inicial: os mesmos blocos, mas no topo do payload.
PAYLOAD_PLANO = {
    "ip": "10.25.4.2",
    "timestamp": "2026-07-31 10:48:45",
    "temps": {"T1": 12.0},
    "pressoes": {"P1": 4.0},
    "medidor": {"EnergiaAtivaTotal_output": 100.0},
}


class NormalizacaoDoPayloadTests(SimpleTestCase):
    def test_payload_do_raspberry_e_aceite(self):
        dados, erro = mqtt_listener._normalizar_payload(PAYLOAD_RASPBERRY)
        self.assertIsNone(erro)
        self.assertEqual(dados["ip"], "10.0.0.1")
        self.assertEqual(dados["timestamp"], "2026-07-31 10:48:45")
        self.assertEqual(dados["temps"]["T2"], 71.0)
        self.assertEqual(dados["pressoes"]["P2"], 19.374232959638164)
        self.assertEqual(dados["medidor"]["EnergiaAtivaTotal_output"], 15874.462890625)

    def test_metadados_do_payload_ficam_disponiveis(self):
        dados, _ = mqtt_listener._normalizar_payload(PAYLOAD_RASPBERRY)
        self.assertEqual((dados["chiller"], dados["fluido"], dados["ciclo"]), ("Chiller 1", "R407C", "verao"))

    def test_forma_antiga_continua_a_funcionar(self):
        dados, erro = mqtt_listener._normalizar_payload(PAYLOAD_PLANO)
        self.assertIsNone(erro)
        self.assertEqual(dados["temps"], {"T1": 12.0})
        self.assertIsNone(dados["chiller"])

    def test_sem_blocos_de_leituras_e_rejeitado(self):
        _, erro = mqtt_listener._normalizar_payload({"ip": "10.0.0.1", "timestamp": "2026-07-31 10:48:45"})
        self.assertIn("temps", erro)

    def test_sem_ip_e_rejeitado(self):
        payload = {k: v for k, v in PAYLOAD_RASPBERRY.items() if k != "ip"}
        _, erro = mqtt_listener._normalizar_payload(payload)
        self.assertIn("ip", erro)

    def test_values_com_tipo_errado_e_rejeitado(self):
        _, erro = mqtt_listener._normalizar_payload({"ip": "1", "timestamp": "t", "values": "nada"})
        self.assertIn("values", erro)

    def test_payload_que_nao_e_objeto_e_rejeitado(self):
        self.assertIsNotNone(mqtt_listener._normalizar_payload([1, 2, 3])[1])


class TopicosTests(SimpleTestCase):
    def test_lista_separada_por_virgulas(self):
        self.assertEqual(
            mqtt_listener._topicos("iot/raspberry-aylon/#, chillers/+/telemetria"),
            ["iot/raspberry-aylon/#", "chillers/+/telemetria"],
        )

    def test_vazio_da_lista_vazia(self):
        self.assertEqual(mqtt_listener._topicos(""), [])
        self.assertEqual(mqtt_listener._topicos(None), [])


class MetadadosDeChillerNaoRegistadoTests(SimpleTestCase):
    """Um IP que não está na BD não pode fazer perder a leitura: grava-se com os
    metadados do próprio payload."""

    def test_usa_o_que_o_payload_traz(self):
        dados, _ = mqtt_listener._normalizar_payload(PAYLOAD_RASPBERRY)
        meta = mqtt_listener.Command._meta_do_payload(dados, "10.0.0.1", avisar=False)
        self.assertEqual(meta, {"nome": "Chiller 1", "gas": "R407C", "ciclo": "verao"})

    def test_sem_metadados_no_payload_cai_nos_defaults(self):
        dados, _ = mqtt_listener._normalizar_payload(PAYLOAD_PLANO)
        meta = mqtt_listener.Command._meta_do_payload(dados, "10.25.4.2", avisar=False)
        self.assertEqual(meta, {"nome": "10.25.4.2", "gas": telemetry.DEFAULT_FLUIDO, "ciclo": "verao"})


class CacheDeMetadadosTests(SimpleTestCase):
    def _cache(self, chillers):
        """Cache com get_active_chillers() falso, devolvendo (cache, contador_de_queries)."""
        chamadas = []

        def falso():
            chamadas.append(1)
            return dict(chillers)

        cache = mqtt_listener.ChillerMetadataCache(300)
        patcher = mock.patch.object(telemetry, "get_active_chillers", falso)
        patcher.start()
        self.addCleanup(patcher.stop)
        return cache, chamadas

    def test_ip_registado_resolve_sem_query(self):
        cache, chamadas = self._cache({"10.0.0.1": {"nome": "C1", "gas": "R407C", "ciclo": "verao"}})
        cache.reload()
        meta, primeira_falha = cache.resolve("10.0.0.1")
        self.assertEqual(meta["nome"], "C1")
        self.assertFalse(primeira_falha)
        self.assertEqual(len(chamadas), 1)  # só o reload inicial

    def test_ip_desconhecido_so_forca_um_refresh(self):
        # Sem esta travagem, um chiller que publica a cada 30s e não está registado
        # gerava uma query ao Postgres por mensagem, para sempre.
        cache, chamadas = self._cache({})
        cache.reload()
        self.assertEqual(cache.resolve("10.0.0.9"), (None, True))
        self.assertEqual(len(chamadas), 2)
        self.assertEqual(cache.resolve("10.0.0.9"), (None, False))
        self.assertEqual(len(chamadas), 2)

    def test_ip_registado_mais_tarde_deixa_de_ser_desconhecido(self):
        chillers = {}
        cache, _ = self._cache(chillers)
        cache.reload()
        self.assertEqual(cache.resolve("10.0.0.9"), (None, True))
        chillers["10.0.0.9"] = {"nome": "C9", "gas": "R404A", "ciclo": "inverno"}
        cache.reload()  # o refresh periódico apanha o chiller novo
        meta, _ = cache.resolve("10.0.0.9")
        self.assertEqual(meta["nome"], "C9")


# --- Métricas do ciclo -----------------------------------------------------------

# Ciclo de arrefecimento R407C fisicamente coerente.
TEMPS_COERENTE = {"T1": 12.0, "T2": 78.0, "T3": 40.0, "T4": 8.0, "T5": 10.7}
PRESSOES_COERENTE = {"P1": 4.0, "P2": 19.5}

# Valores tal como vieram do hardware a 2026-07-24 18:55:50.
TEMPS_REAL_2407 = {"T1": 18.4, "T2": 73.5, "T3": 42.8, "T4": 43.1, "T5": 10.7}
PRESSOES_REAL_2407 = {"P1": 17.30580951198996, "P2": 19.512127856148044}


class MetricasCicloTests(SimpleTestCase):
    def test_ciclo_coerente_calcula_cop_sem_avisos(self):
        r = telemetry.calcular_metricas_ciclo(
            TEMPS_COERENTE, PRESSOES_COERENTE, 'ARREFECIMENTO', 'R407C'
        )
        self.assertIsNotNone(r['rendimento'])
        self.assertGreater(r['rendimento'], 1.0)
        self.assertEqual(r['aviso'], "")
        self.assertTrue(all(v is not None for v in r['entalpias'].values()))

    def test_dados_reais_de_2407_explicam_a_ausencia_de_cop(self):
        """O caso que motivou este aviso: sem ele o documento ficava com rendimento a
        null e aviso vazio, sem nada que indicasse que o problema é de instrumentação."""
        r = telemetry.calcular_metricas_ciclo(
            TEMPS_REAL_2407, PRESSOES_REAL_2407, 'ARREFECIMENTO', 'R407C'
        )
        self.assertIsNone(r['rendimento'])
        self.assertNotEqual(r['aviso'], "")
        self.assertIn("rácio de pressões implausível", r['aviso'])
        self.assertIn("zona bifásica", r['aviso'])
        self.assertIn("T4", r['aviso'])
        self.assertIsNone(r['entalpias']['h4'])

    def test_racio_de_pressoes_implausivel_e_assinalado(self):
        aviso = telemetry._aviso_racio_pressoes({"P1": 17.3, "P2": 19.5})
        self.assertIsNotNone(aviso)
        self.assertIn("1.12", aviso)

    def test_racio_de_pressoes_saudavel_nao_gera_aviso(self):
        self.assertIsNone(telemetry._aviso_racio_pressoes(PRESSOES_COERENTE))

    def test_sensor_em_falta_e_reportado_como_tal(self):
        temps = dict(TEMPS_COERENTE, T4=None)
        r = telemetry.calcular_metricas_ciclo(temps, PRESSOES_COERENTE, 'ARREFECIMENTO', 'R407C')
        self.assertIsNone(r['rendimento'])
        self.assertIn("falta a leitura", r['aviso'])

    def test_sensores_em_falta_no_payload(self):
        r = telemetry.calcular_metricas_ciclo({"T1": 12.0}, PRESSOES_COERENTE, 'ARREFECIMENTO', 'R407C')
        self.assertIsNone(r['rendimento'])
        self.assertEqual(r['aviso'], "Sensores insuficientes")


# --- Processamento completo ------------------------------------------------------

class ProcessChillerTests(LimpaEstadoGlobalMixin, TestCase):
    IP = "10.0.0.9"

    def setUp(self):
        super().setUp()
        self.cliente = Cliente.objects.create(nome="Cliente Teste", email="teste@exemplo.pt")
        self.chiller = Chiller.objects.create(
            nome="Chiller Teste",
            localizacao="Bragança",
            ipcontrolador=self.IP,
            status="ligado",
            gas="R407C",
            ciclo="verao",
            horas_funcionamento=0,
            idCliente=self.cliente,
        )
        self.colecao = ColecaoFalsa()

    def _processar(self, correntes, timestamp="2026-07-27 15:00:00", recebido=None,
                   temps=None, pressoes=None, kwh=1000.0):
        return telemetry.process_chiller(
            ip=self.IP,
            nome=self.chiller.nome,
            timestamp=timestamp,
            raw_data={
                "temps": dict(temps or TEMPS_COERENTE),
                "pressoes": dict(pressoes or PRESSOES_COERENTE),
                "medidor": {
                    "Corrente_L1_output": correntes[0],
                    "Corrente_L2_output": correntes[1],
                    "Corrente_L3_output": correntes[2],
                    "Tensao_L1_L2_output": 415.0,
                    "Tensao_L2_L3_output": 415.0,
                    "Tensao_L3_L1_output": 415.0,
                    "EnergiaAtivaTotal_output": kwh,
                },
            },
            collection=self.colecao,
            fluido_db=self.chiller.gas,
            ciclo_db=self.chiller.ciclo,
            received_at=recebido or datetime(2026, 7, 27, 15, 0, 0),
        )

    def test_corrente_baixa_e_standby_e_nao_soma_horas(self):
        doc = self._processar([0.4, 0.35, 0.42])
        self.assertEqual(doc["values"]["medidor"]["estado_chiller"], "standby")
        self.chiller.refresh_from_db()
        self.assertEqual(float(self.chiller.horas_funcionamento), 0.0)

    def test_corrente_alta_e_ativo(self):
        doc = self._processar([26.0, 26.2, 24.9])
        self.assertEqual(doc["values"]["medidor"]["estado_chiller"], "ativo")
        self.assertIsNotNone(doc["values"]["rendimento"])

    def test_horas_somam_o_intervalo_entre_leituras_ativas(self):
        self._processar([26.0, 26.2, 24.9], timestamp="2026-07-27 15:00:00")
        self._processar([26.0, 26.2, 24.9], timestamp="2026-07-27 15:30:00",
                        recebido=datetime(2026, 7, 27, 15, 30, 0))
        self.chiller.refresh_from_db()
        self.assertAlmostEqual(float(self.chiller.horas_funcionamento), 0.5, places=2)

    def test_documento_guarda_a_hora_de_rececao_e_o_timestamp_original(self):
        """Mesmo com o relógio do chiller três horas adiantado, o payload original
        fica intacto no documento e a hora de receção é gravada à parte — é por ela
        que a API ordena."""
        doc = self._processar(
            [26.0, 26.2, 24.9],
            timestamp="2026-07-27 18:00:00",
            recebido=datetime(2026, 7, 27, 15, 0, 0),
        )
        self.assertEqual(doc["timestamp"], "2026-07-27 18:00:00")
        self.assertEqual(doc["recebido_em"], "2026-07-27 15:00:00")

    def test_medias_de_tensao_e_corrente_sao_calculadas(self):
        doc = self._processar([26.0, 26.0, 25.0])
        medidor = doc["values"]["medidor"]
        self.assertAlmostEqual(medidor["MediaTensoes_output"], 415.0)
        self.assertAlmostEqual(medidor["MediaCorrentes_output"], (26.0 + 26.0 + 25.0) / 3)

    def test_documento_e_inserido_na_colecao(self):
        self._processar([26.0, 26.2, 24.9])
        self.assertEqual(len(self.colecao.documentos), 1)
        self.assertEqual(self.colecao.documentos[0]["ip"], self.IP)

    def test_dados_reais_de_2407_gravam_o_aviso_no_documento(self):
        doc = self._processar(
            [25.1, 25.2, 24.2], temps=TEMPS_REAL_2407, pressoes=PRESSOES_REAL_2407
        )
        self.assertIsNone(doc["values"]["rendimento"])
        self.assertIn("rácio de pressões implausível", doc["values"]["aviso"])


class IncrementHoursTests(TestCase):
    def test_soma_horas_a_partir_de_null(self):
        cliente = Cliente.objects.create(nome="C", email="c@exemplo.pt")
        chiller = Chiller.objects.create(
            nome="X", localizacao="Y", ipcontrolador="10.0.0.50",
            status="ligado", horas_funcionamento=None, idCliente=cliente,
        )
        telemetry.increment_hours("10.0.0.50", 1.5)
        chiller.refresh_from_db()
        self.assertAlmostEqual(float(chiller.horas_funcionamento), 1.5, places=2)

    def test_ip_desconhecido_nao_rebenta(self):
        telemetry.increment_hours("192.0.2.123", 1.0)  # não deve levantar exceção


class GetActiveChillersTests(TestCase):
    def test_exclui_desativados_e_aplica_defaults(self):
        cliente = Cliente.objects.create(nome="C", email="c@exemplo.pt")
        # gas aceita null; ciclo é NOT NULL na base de dados, por isso o caso a testar
        # aqui é o da string vazia — é essa que o fallback de get_active_chillers apanha.
        Chiller.objects.create(
            nome="Ativo", localizacao="L", ipcontrolador="10.0.0.60",
            status="ligado", gas=None, ciclo="", idCliente=cliente,
        )
        Chiller.objects.create(
            nome="Fora", localizacao="L", ipcontrolador="10.0.0.61",
            status="desativado", gas="R407C", ciclo="verao", idCliente=cliente,
        )
        ativos = telemetry.get_active_chillers()
        self.assertIn("10.0.0.60", ativos)
        self.assertNotIn("10.0.0.61", ativos)
        # Sem gás nem ciclo definidos, aplicam-se os valores por omissão.
        self.assertEqual(ativos["10.0.0.60"]["gas"], telemetry.DEFAULT_FLUIDO)
        self.assertEqual(ativos["10.0.0.60"]["ciclo"], "verao")


# --- Autorização das views do dashboard ------------------------------------------

class AutorizacaoDashboardTests(TestCase):
    """Cada teste corresponde a um acesso que era possível antes desta suite existir.

    Usa RequestFactory em vez do test client de propósito. O test client instrumenta a
    renderização de templates (store_rendered_templates faz copy() do Context), e isso
    rebenta no Python 3.14 com "'super' object has no attribute 'dicts'" — um problema
    do Django 5.0.6 com o 3.14, não do código em teste. Com o RequestFactory chamamos a
    view diretamente, a TemplateResponse volta sem ser renderizada, e os testes passam
    tanto aqui como no Python 3.12 do container.

    O critério de resposta não é uniforme de propósito: ler um recurso de outro cliente dá
    Http404, para não confirmar que existe; escrever indevidamente dá PermissionDenied,
    porque aí o problema não é revelar a existência mas recusar a operação.
    """

    def setUp(self):
        self.factory = RequestFactory()
        self.cliente_a = Cliente.objects.create(nome="Cliente A", email="a@exemplo.pt")
        self.cliente_b = Cliente.objects.create(nome="Cliente B", email="b@exemplo.pt")
        self.chiller_b = Chiller.objects.create(
            nome="Chiller do B", localizacao="L", ipcontrolador="10.9.9.9",
            status="ligado", gas="R407C", ciclo="verao", idCliente=self.cliente_b,
        )
        # Utilizador do cliente A: autenticado, mas só tem direito ao cliente A.
        self.user_a = User.objects.create_user("user_a", password="x")
        ClienteProfile.objects.create(user=self.user_a, cliente=self.cliente_a)
        self.admin = User.objects.create_superuser("admin_teste", password="x")

    def _get(self, view, user, **params):
        request = self.factory.get("/", params)
        request.user = user
        return view(request)

    def _post(self, view, user, dados=None, corpo_json=None):
        if corpo_json is not None:
            request = self.factory.post("/", data=json.dumps(corpo_json), content_type="application/json")
        else:
            request = self.factory.post("/", dados or {})
        request.user = user
        # O RequestFactory não passa pelo middleware das mensagens, e o caminho legítimo
        # do dashboard chama messages.success(). Usa-se CookieStorage por não exigir sessão.
        request._messages = CookieStorage(request)
        return view(request)

    # --- leitura de outro cliente: Http404 ---

    def test_cliente_nao_ve_dashboard_de_outro_cliente(self):
        with self.assertRaises(Http404):
            self._get(DashboardView.as_view(), self.user_a, cliente_id=self.cliente_b.idCliente)

    def test_cliente_ve_o_seu_proprio_dashboard(self):
        r = self._get(DashboardView.as_view(), self.user_a, cliente_id=self.cliente_a.idCliente)
        self.assertEqual(r.status_code, 200)

    def test_superutilizador_ve_qualquer_cliente(self):
        r = self._get(DashboardView.as_view(), self.admin, cliente_id=self.cliente_b.idCliente)
        self.assertEqual(r.status_code, 200)

    def test_sem_cliente_id_continua_a_funcionar(self):
        # O dashboard sem cliente escolhido não deve ser bloqueado.
        r = self._get(DashboardView.as_view(), self.user_a)
        self.assertEqual(r.status_code, 200)

    def test_relatorio_de_outro_cliente_bloqueado(self):
        with self.assertRaises(Http404):
            self._get(RelatorioView.as_view(), self.user_a, cliente_id=self.cliente_b.idCliente)

    def test_livro_de_obra_de_outro_cliente_bloqueado(self):
        with self.assertRaises(Http404):
            self._get(LivroObraView.as_view(), self.user_a, cliente_id=self.cliente_b.idCliente)

    def test_logo_de_outro_cliente_da_404(self):
        request = self.factory.get("/")
        request.user = self.user_a
        with self.assertRaises(Http404):
            ClienteLogoView.as_view()(request, cliente_id=self.cliente_b.idCliente)

    def test_detalhe_de_outro_cliente_nao_expoe_email(self):
        with self.assertRaises(Http404):
            self._get(ClienteChillerDetailView.as_view(), self.user_a, id=self.cliente_b.idCliente)

    def test_tabela_de_clientes_e_so_para_internos(self):
        with self.assertRaises(PermissionDenied):
            self._get(TableClientsView.as_view(), self.user_a)

    # --- escrita indevida: PermissionDenied, e o objeto sobrevive ---

    def test_cliente_nao_apaga_chiller_de_outro_pelo_dashboard(self):
        with self.assertRaises(PermissionDenied):
            self._post(DashboardView.as_view(), self.user_a, {"delete_id": self.chiller_b.idChiller})
        self.assertTrue(Chiller.objects.filter(pk=self.chiller_b.pk).exists())

    def test_cliente_nao_apaga_outro_cliente(self):
        with self.assertRaises(PermissionDenied):
            self._post(ClienteDeleteView.as_view(), self.user_a, corpo_json={"id": self.cliente_b.idCliente})
        self.assertTrue(Cliente.objects.filter(pk=self.cliente_b.pk).exists())

    def test_cliente_nao_altera_outro_cliente(self):
        with self.assertRaises(PermissionDenied):
            self._post(ClienteUpdateView.as_view(), self.user_a, {
                "cliente_id": self.cliente_b.idCliente, "nome": "Invadido",
                "email": "x@x.pt", "telefone": "1", "localidade": "L", "nif": "1",
            })
        self.cliente_b.refresh_from_db()
        self.assertEqual(self.cliente_b.nome, "Cliente B")

    def test_cliente_nao_registra_intervencao(self):
        with self.assertRaises(PermissionDenied):
            self._post(RegistarIntervencaoView.as_view(), self.user_a, {
                "chiller_id": self.chiller_b.idChiller, "tecnico": "X",
                "tipo": "preventiva", "categoria": "geral", "descricao": "d",
            })
        self.assertEqual(Intervencao.objects.count(), 0)

    def test_superutilizador_apaga_chiller(self):
        # A contraprova: a restrição não pode ter quebrado o fluxo legítimo.
        self._post(DashboardView.as_view(), self.admin, {"delete_id": self.chiller_b.idChiller})
        self.assertFalse(Chiller.objects.filter(pk=self.chiller_b.pk).exists())


class AutorizacaoAreaClienteTests(TestCase):
    """A área de cliente deriva quase sempre o cliente de request.user.clienteprofile, que é
    o padrão certo. Estes dois casos eram a exceção."""

    def setUp(self):
        self.factory = RequestFactory()
        self.cliente_a = Cliente.objects.create(nome="Cliente A", email="a@exemplo.pt")
        self.cliente_b = Cliente.objects.create(nome="Cliente B", email="b@exemplo.pt")
        self.user_a = User.objects.create_user("user_a_cli", password="x")
        ClienteProfile.objects.create(user=self.user_a, cliente=self.cliente_a)

    def test_relatorio_de_outro_cliente_bloqueado(self):
        request = self.factory.get("/", {"cliente_id": self.cliente_b.idCliente})
        request.user = self.user_a
        with self.assertRaises(Http404):
            ClienteRelatorioAreaView.as_view()(request)

    def test_logo_da_area_de_cliente_exige_posse(self):
        request = self.factory.get("/")
        request.user = self.user_a
        with self.assertRaises(Http404):
            ClienteLogoAreaView.as_view()(request, cliente_id=self.cliente_b.idCliente)


# --- Página de logs de variáveis -------------------------------------------------

class LogVariaveisTemplateTests(TestCase):
    """A page_5 do dashboard e a logs.html da área de cliente eram dois ficheiros byte a
    byte iguais, com o gráfico e o JavaScript duplicados. Corrigir um não corrigia o outro
    e foi assim que as duas ficaram com o mesmo gráfico partido.

    Agora as duas incluem templates/partials/_log_variaveis*.html. Estes testes prendem
    essa partilha e os defeitos concretos que o gráfico tinha, para não voltarem se alguém
    recolar a versão antiga.

    Não é preciso MongoDB: sem ele a view apanha a exceção e devolve a lista de variáveis
    vazia, e o que se está a verificar é o template.
    """

    # Elementos que o JavaScript procura pelo id. Se um deles desaparecer do markup, o
    # script deixa de funcionar em silêncio.
    ANCORAS = (
        'id="logGraficos"', 'id="logResumo"', 'id="logEstado"',
        'id="logTabelaWrap"', 'id="dadosChiller"', 'id="variableForm"',
    )

    # Cada um destes esteve na versão anterior e é um defeito conhecido.
    REGRESSOES = {
        "cdn.jsdelivr.net": "o ApexCharts tem de vir do tema, não de um CDN externo — o "
                            "servidor está numa rede interna",
        "height: 600": "a altura das opções tem de caber no cartão, senão o eixo do tempo "
                       "fica fora da área visível",
        "curve: 'smooth'": "a spline inventa valores entre leituras",
        "strokeDashArray: 4": "a grelha tracejada lê-se como limiar",
    }

    def setUp(self):
        self.factory = RequestFactory()
        self.cliente = Cliente.objects.create(nome="Ibis Teste", email="t@exemplo.pt")
        self.chiller = Chiller.objects.create(
            nome="Chiller de teste", localizacao="L", ipcontrolador="10.25.4.2",
            status="ligado", gas="R407C", ciclo="verao", idCliente=self.cliente,
        )
        self.admin = User.objects.create_superuser("admin_logs", password="x")
        self.user_cliente = User.objects.create_user("user_logs", password="x")
        ClienteProfile.objects.create(user=self.user_cliente, cliente=self.cliente)

    def _render(self, view, user, **params):
        request = self.factory.get("/", params)
        request.user = user
        # O LocaleMiddleware é que costuma pôr isto; com o RequestFactory não corre e o
        # context processor da língua rebentava.
        request.LANGUAGE_CODE = "pt-pt"
        resposta = view(request)
        resposta.render()
        return resposta.content.decode()

    def _html_dashboard(self):
        return self._render(
            DashboardVariableLogView.as_view(template_name="page_5.html"), self.admin,
            cliente_id=self.cliente.idCliente, chiller_id=self.chiller.idChiller,
        )

    def _html_area_cliente(self):
        return self._render(
            ClienteVariableLogAreaView.as_view(), self.user_cliente,
            chiller_id=self.chiller.idChiller,
        )

    def test_as_duas_paginas_trazem_as_mesmas_ancoras(self):
        for nome, html in (("page_5", self._html_dashboard()),
                           ("logs.html", self._html_area_cliente())):
            for ancora in self.ANCORAS:
                with self.subTest(pagina=nome, ancora=ancora):
                    self.assertIn(ancora, html)

    def test_nenhuma_das_paginas_regride_para_a_versao_antiga(self):
        for nome, html in (("page_5", self._html_dashboard()),
                           ("logs.html", self._html_area_cliente())):
            for marca, porque in self.REGRESSOES.items():
                with self.subTest(pagina=nome, marca=marca):
                    self.assertNotIn(marca, html, porque)

    def test_o_ip_do_chiller_vai_em_json_script_e_nao_interpolado_no_javascript(self):
        # Interpolar um valor da base de dados dentro de uma string JS é como o IP era
        # passado antes; o json_script escapa-o.
        html = self._html_dashboard()
        self.assertIn('<script id="dadosChiller" type="application/json">"10.25.4.2"</script>', html)

    def test_sem_chiller_a_pagina_nao_arranca_o_javascript(self):
        html = self._render(
            DashboardVariableLogView.as_view(template_name="page_5.html"), self.admin,
            cliente_id=self.cliente.idCliente,
        )
        self.assertIn("Selecione um chiller", html)
        # Sem chiller não há o div dos gráficos, e o script sai logo à cabeça.
        self.assertNotIn('id="logGraficos"', html)

    def test_os_dois_templates_incluem_o_partial_em_vez_de_o_copiarem(self):
        raiz = Path(settings.BASE_DIR)
        for caminho in ("apps/dashboard/templates/page_5.html",
                        "apps/cliente/templates/logs.html"):
            with self.subTest(template=caminho):
                fonte = (raiz / caminho).read_text()
                self.assertIn('{% include "partials/_log_variaveis.html" %}', fonte)
                self.assertIn('{% include "partials/_log_variaveis_js.html" %}', fonte)
                # Se voltar a ter o corpo lá dentro, volta a divergir da outra.
                self.assertNotIn("<script>", fonte)


# --- Achatamento da resposta da API ----------------------------------------------

class MongoFalso:
    """Faz de MongoClient para as views que leem a coleção "values".

    Suporta só o que elas usam: client[db]["values"], find().sort().limit() e find_one().
    """

    def __init__(self, documentos):
        self.documentos = documentos

    # MongoClient(...) -> client[db] -> db["values"]
    def __call__(self, *a, **kw):
        return self

    def __getitem__(self, _nome):
        return self

    def find(self, _query=None):
        return self

    def sort(self, *a, **kw):
        return self

    def limit(self, *a, **kw):
        return list(self.documentos)

    def find_one(self, _query=None, sort=None):
        return self.documentos[0] if self.documentos else None

    def close(self):
        pass


# Documento com a forma real: as entalpias e os estados usam as mesmas chaves h1..h4.
DOCUMENTO = {
    "ip": "10.25.4.2",
    "chiller": "Chiller de teste",
    "timestamp": "2026-07-30 10:38:27",
    "recebido_em": "2026-07-30 10:38:27",
    "ciclo": "verao",
    "values": {
        "temps": {"T1": 12.06, "T2": 77.86},
        "pressoes": {"P1": 4.0, "P2": 19.5},
        "medidor": {
            "Corrente_L1_output": 26.08,
            "EnergiaAtivaParcial_output": 14845.96,
            "estado_chiller": "ativo",
            "custo_hora": 0.2975,
        },
        "entalpias": {"h1": 419.72, "h2": 460.53, "h3": 260.18, "h4": 416.21},
        "estados": {
            "h1": "Vapor Superaquecido", "h2": "Vapor Superaquecido",
            "h3": "Líquido Sub-resfriado", "h4": "Vapor Superaquecido",
        },
        "rendimento": 3.5552,
        "aviso": "",
    },
}


class AchatamentoDaApiTests(TestCase):
    """As secções "entalpias" e "estados" do documento usam as MESMAS chaves (h1..h4):
    uma tem o número, a outra a fase do fluido.

    Achatadas para a raiz sem distinção, a que vinha depois no documento ganhava e o h1
    chegava ao frontend como a string "Vapor Superaquecido". As entalpias — o resultado de
    todo o cálculo termodinâmico — ficavam inalcançáveis pela API e por isso escondidas da
    lista de variáveis.
    """

    def setUp(self):
        self.factory = RequestFactory()
        self.cliente = Cliente.objects.create(nome="Ibis API", email="api@exemplo.pt")
        self.chiller = Chiller.objects.create(
            nome="Chiller de teste", localizacao="L", ipcontrolador="10.25.4.2",
            status="ligado", gas="R407C", ciclo="verao", idCliente=self.cliente,
        )
        self.admin = User.objects.create_superuser("admin_api", password="x")
        self.mongo = MongoFalso([DOCUMENTO])

    def _leitura(self):
        request = self.factory.get("/", {"ip": "10.25.4.2"})
        request.user = self.admin
        with mock.patch("apps.dashboard.views.MongoClient", self.mongo):
            resposta = SensorDataAPIView.as_view()(request)
        resposta.render()
        return json.loads(resposta.content.decode())["sensor_data"][0]

    def test_entalpias_chegam_como_numero(self):
        leitura = self._leitura()
        for chave, esperado in (("h1", 419.72), ("h2", 460.53), ("h3", 260.18), ("h4", 416.21)):
            with self.subTest(chave=chave):
                self.assertEqual(leitura[chave], esperado)

    def test_fase_do_fluido_vai_com_prefixo_e_nao_sobrepoe_a_entalpia(self):
        leitura = self._leitura()
        self.assertEqual(leitura["estado_h1"], "Vapor Superaquecido")
        self.assertEqual(leitura["estado_h3"], "Líquido Sub-resfriado")
        self.assertNotIsInstance(leitura["h1"], str)

    def test_o_resto_do_achatamento_nao_muda(self):
        leitura = self._leitura()
        self.assertEqual(leitura["T1"], 12.06)
        self.assertEqual(leitura["P2"], 19.5)
        self.assertEqual(leitura["Corrente_L1_output"], 26.08)
        self.assertEqual(leitura["rendimento"], 3.5552)
        self.assertEqual(leitura["ciclo"], "verao")
        self.assertEqual(leitura["IP"], "10.25.4.2")


class ListaDeVariaveisTests(TestCase):
    """A lista de variáveis escolhíveis é filtrada pelo TIPO do valor e não por uma lista
    de nomes: o que não é número não se desenha num gráfico.

    As duas views — dashboard e área de cliente — têm de devolver a mesma lista, porque
    partilham o template desde a correção do gráfico. Uma diferença aqui só se notaria
    como uma variável que aparece a um utilizador e não ao outro.
    """

    def setUp(self):
        self.factory = RequestFactory()
        self.cliente = Cliente.objects.create(nome="Ibis Lista", email="l@exemplo.pt")
        self.chiller = Chiller.objects.create(
            nome="Chiller de teste", localizacao="L", ipcontrolador="10.25.4.2",
            status="ligado", gas="R407C", ciclo="verao", idCliente=self.cliente,
        )
        self.admin = User.objects.create_superuser("admin_lista", password="x")
        self.user_cliente = User.objects.create_user("user_lista", password="x")
        ClienteProfile.objects.create(user=self.user_cliente, cliente=self.cliente)
        self.mongo = MongoFalso([DOCUMENTO])

    def _variaveis(self, modulo, vista, user, **params):
        request = self.factory.get("/", params)
        request.user = user
        request.LANGUAGE_CODE = "pt-pt"
        instancia = vista()
        instancia.setup(request)
        with mock.patch(f"{modulo}.MongoClient", self.mongo):
            return instancia.get_context_data()["variaveis"]

    def _do_dashboard(self):
        return self._variaveis(
            "apps.dashboard.views", DashboardVariableLogView, self.admin,
            cliente_id=self.cliente.idCliente, chiller_id=self.chiller.idChiller,
        )

    def _da_area_cliente(self):
        return self._variaveis(
            "apps.cliente.views", ClienteVariableLogAreaView, self.user_cliente,
            chiller_id=self.chiller.idChiller,
        )

    def test_as_entalpias_aparecem(self):
        # Estavam numa lista negra por chegarem como texto — o sintoma da colisão, não a
        # causa. Resolvida a colisão, são leituras como as outras.
        for lista in (self._do_dashboard(), self._da_area_cliente()):
            for chave in ("h1", "h2", "h3", "h4"):
                self.assertIn(chave, lista)

    def test_o_que_e_texto_fica_de_fora(self):
        for lista in (self._do_dashboard(), self._da_area_cliente()):
            for chave in ("estado_chiller", "aviso", "estado_h1"):
                self.assertNotIn(chave, lista)

    def test_as_duas_views_devolvem_a_mesma_lista(self):
        self.assertEqual(self._do_dashboard(), self._da_area_cliente())

    def test_a_lista_vem_ordenada_e_sem_repetidos(self):
        lista = self._do_dashboard()
        self.assertEqual(lista, sorted(lista))
        self.assertEqual(len(lista), len(set(lista)))
