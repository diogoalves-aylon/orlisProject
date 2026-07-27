"""
Testes do processamento de telemetria (apps.dashboard.services.telemetry).

O módulo é quase todo puro, o que o torna barato de testar sem broker nem hardware.
As exceções são increment_hours() e get_active_chillers(), que tocam no Postgres, e
process_chiller(), que grava no MongoDB — para esse último basta uma coleção falsa,
porque só se lhe chama insert_one().

Atenção ao estado global: memoria_energia e ultima_leitura_por_chiller vivem no módulo
e sobrevivem entre testes. Todos os casos abaixo limpam-nos no setUp.
"""
from datetime import datetime

from django.test import SimpleTestCase, TestCase

from apps.dashboard.models import Chiller, Cliente
from apps.dashboard.services import telemetry


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
