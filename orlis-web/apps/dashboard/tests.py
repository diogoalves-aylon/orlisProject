"""
Testes do processamento de telemetria (apps.dashboard.services.telemetry).

O módulo é quase todo puro, o que o torna barato de testar sem broker nem hardware.
As exceções são increment_hours() e get_active_chillers(), que tocam no Postgres, e
process_chiller(), que grava no MongoDB — para esse último basta uma coleção falsa,
porque só se lhe chama insert_one().

Atenção ao estado global: memoria_energia e ultima_leitura_por_chiller vivem no módulo
e sobrevivem entre testes. Todos os casos abaixo limpam-nos no setUp.
"""
import json
from datetime import datetime

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
)
from apps.dashboard.models import Chiller, Cliente, Intervencao
from apps.dashboard.services import telemetry
from apps.dashboard.views import (
    ClienteChillerDetailView, ClienteDeleteView, ClienteLogoView, ClienteUpdateView,
    DashboardView, LivroObraView, RegistarIntervencaoView, RelatorioView, TableClientsView,
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
