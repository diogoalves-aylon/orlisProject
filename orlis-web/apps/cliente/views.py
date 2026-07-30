from django.views.generic import TemplateView
from django.contrib.auth.mixins import LoginRequiredMixin
from apps.dashboard.permissions import ClienteQueryStringMixin, pode_ver_cliente
from django.conf import settings
from apps.dashboard.models import Chiller
from django.shortcuts import redirect
from django.contrib.auth import login
from django.contrib.auth.forms import AuthenticationForm
from django.shortcuts import redirect, render
from django.views import View
from django.shortcuts import redirect
from django.contrib.auth import login
from web_project import TemplateLayout
from pymongo import MongoClient
from apps.cliente.models import Cliente
import os
from django.template.loader import render_to_string
import weasyprint
from datetime import datetime, timedelta
from django.http import HttpResponse, Http404
from apps.cliente.models import Cliente
from apps.dashboard.models import Chiller
from django.views.generic.base import TemplateResponseMixin


# Project Imports
from apps.dashboard.models import Chiller, Intervencao

from django.views.generic import TemplateView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views import View
from django.shortcuts import redirect, render, get_object_or_404
from django.contrib.auth import login
from django.contrib.auth.forms import AuthenticationForm
from django.http import HttpResponse, Http404
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from datetime import datetime, timedelta
import weasyprint
from pymongo import MongoClient, DESCENDING
from django.contrib import messages



# --- Helper: Safe Float ---
def safe_float(val):
    if val is None or val == "": return 0.0
    if isinstance(val, (int, float)): return float(val)
    try: return float(str(val).replace(',', '.'))
    except (TypeError, ValueError): return 0.0

# --- Helper: Dados Mongo ---
def get_mongo_data(ip_chiller):
    data = {
        'status': 'offline', 'ciclo': 'N/A', 'rendimento': 0, 
        'medidor': {'Custo': 0.0, 'PotenciaAtivaTotal_kW': 0.0}
    }
    if not ip_chiller: return data
    try:
        client = MongoClient(settings.MONGO_URL, serverSelectionTimeoutMS=2000)
        db = client[settings.MONGO_DB_NAME]
        collection = db["values"]
        last_doc = collection.find_one({"ip": ip_chiller}, sort=[('timestamp', -1)])
        
        if last_doc:
            vals = last_doc.get('values', {})
            medidor = vals.get('medidor', {})
            data['status'] = vals.get('estado_chiller', 'offline')
            data['ciclo'] = vals.get('ciclo', 'N/A')
            data['rendimento'] = vals.get('rendimento', 0)
            data['medidor']['Custo'] = medidor.get('custo', 0) or medidor.get('Custo_hora', 0)
            data['medidor']['PotenciaAtivaTotal_kW'] = medidor.get('PotenciaAtivaTotal_kW', 0) or medidor.get('PotenciaAbsorbida_kW', 0)
        client.close()
    except Exception as e:
        print(f"Erro Mongo: {e}")
    return data

class CustomLoginView(View):
    def get(self, request):
        form = AuthenticationForm()
        return render(request, 'login', {'form': form})

    def post(self, request):
        form = AuthenticationForm(data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)

            if user.is_superuser:
                return redirect('/dashboard/')
            else:
                return redirect('/cliente/meus-chillers/')

        return render(request, 'teu_template_login.html', {'form': form})
        
class ClienteChillersView(LoginRequiredMixin, View, TemplateResponseMixin):
    template_name = 'chillers.html'

    def get_context_data(self, **kwargs):
        # ... (O teu código get_context_data mantém-se igual) ...
        context = kwargs or {}
        layout_context = TemplateLayout.init(self, context)
        context.update(layout_context)

        try:
            cliente = self.request.user.clienteprofile.cliente
            chillers = Chiller.objects.filter(idCliente=cliente)
        except AttributeError:
            cliente = None
            chillers = []

        context.update({
            'cliente': cliente,
            'chillers': chillers,
            'layout_path': context.get('layout_path', 'base.html')
        })
        return context

    def get(self, request, *args, **kwargs):
        context = self.get_context_data()
        return self.render_to_response(context)

    def post(self, request, *args, **kwargs):
        try:
            cliente = request.user.clienteprofile.cliente
        except Exception as e:
            print(f"[ClienteChillersView.post] Erro a obter clienteprofile: {e}")
            messages.error(request, "Erro: Perfil de cliente não encontrado.")
            return redirect(request.path)

        # --- AÇÃO: APAGAR ---
        if 'delete_id' in request.POST:
            try:
                Chiller.objects.filter(idChiller=request.POST['delete_id'], idCliente=cliente).delete()
                messages.success(request, "Chiller apagado com sucesso!")
            except Exception as e:
                messages.error(request, "Erro ao apagar chiller.")
            return redirect(request.path)

        # --- AÇÃO: EDITAR ---
        elif 'edit_id' in request.POST:
            chiller = Chiller.objects.filter(idChiller=request.POST['edit_id'], idCliente=cliente).first()
            if chiller:
                chiller.nome = request.POST.get('nome')
                chiller.localizacao = request.POST.get('localizacao')
                chiller.ipcontrolador = request.POST.get('ipcontrolador')
                chiller.marca = request.POST.get('marca')
                chiller.modelo = request.POST.get('modelo')
                chiller.n_serie = request.POST.get('n_serie')
                chiller.gas = request.POST.get('gas')
                chiller.ciclo = request.POST.get('ciclo')
                chiller.status = request.POST.get('status')
                
                lat = request.POST.get('latitude')
                lng = request.POST.get('longitude')
                chiller.latitude = float(lat) if lat else None
                chiller.longitude = float(lng) if lng else None
                
                chiller.save()
                messages.success(request, f"Chiller '{chiller.nome}' atualizado com sucesso!")
            else:
                messages.error(request, "Chiller não encontrado para edição.")
            return redirect(request.path)

        # --- AÇÃO: CRIAR NOVO ---
        nome = request.POST.get('nome')
        localizacao = request.POST.get('localizacao')
        ipcontrolador = request.POST.get('ipcontrolador')

        if nome and localizacao and ipcontrolador:
            lat = request.POST.get('latitude')
            lng = request.POST.get('longitude')
            
            try:
                Chiller.objects.create(
                    nome=nome,
                    localizacao=localizacao,
                    ipcontrolador=ipcontrolador,
                    marca=request.POST.get('marca'),
                    modelo=request.POST.get('modelo'),
                    n_serie=request.POST.get('n_serie'),
                    gas=request.POST.get('gas'),
                    ciclo=request.POST.get('ciclo', 'verao'),
                    status=request.POST.get('status', 'ligado'),
                    latitude=float(lat) if lat else None,
                    longitude=float(lng) if lng else None,
                    idCliente=cliente
                )
                messages.success(request, "Novo chiller registado com sucesso!")
            except Exception as e:
                messages.error(request, f"Erro ao criar chiller: {str(e)}")
        else:
            messages.error(request, "Preencha os campos obrigatórios (Nome, Localização, IP).")

        return redirect(request.path)
    
    
# Conexão Global: Instanciada fora da classe para performance (Connection Pooling)
try:
    # Timeout de 2s para não bloquear o site se o Mongo estiver offline
    mongo_client = MongoClient(settings.MONGO_URL, serverSelectionTimeoutMS=2000)
    db_mongo = mongo_client[settings.MONGO_DB_NAME]
except Exception as e:
    print(f"[apps.cliente.views] Erro ligação MongoDB inicial: {e}")
    db_mongo = None

class ClienteChillerDetailView(LoginRequiredMixin, TemplateView):
    template_name = 'page_2_cliente.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # Inicialização do Layout (Tema)
        layout_context = TemplateLayout.init(self, context)
        context.update(layout_context)

        if not context.get('layout_path'):
            context['layout_path'] = 'base.html'

        try:
            cliente = self.request.user.clienteprofile.cliente
        except AttributeError:
            cliente = None
            
        chillers = Chiller.objects.filter(idCliente=cliente) if cliente else []
        chiller_id = self.request.GET.get('chiller_id')
        chiller = chillers.filter(idChiller=chiller_id).first()

        # Contexto Inicial (Fallback): Valores usados se o MongoDB falhar ou não tiver dados
        context.update({
            'cliente': cliente,
            'chillers': chillers,
            'chiller': chiller,
            "chiller_status_realtime": chiller.status if chiller else "N/A",
            "chiller_ciclo_realtime": "N/A",
            "rendimento": 0,
            "medidor_values": {}
        })

        # Busca dados em tempo real
        if chiller and chiller.ipcontrolador and db_mongo is not None:
            try:
                collection = db_mongo["values"]
                last_doc = collection.find_one(
                    {"ip": chiller.ipcontrolador},
                    sort=[("timestamp", -1)]
                )

                if last_doc:
                    values = last_doc.get("values", {})
                    medidor = values.get("medidor", {})

                    # Lógica do Ciclo: Se Mongo vier vazio, usa o valor do PostgreSQL
                    ciclo_mongo = values.get("ciclo")
                    if ciclo_mongo in [None, "", "null"]:
                        ciclo_final = chiller.ciclo or "N/A"
                    else:
                        ciclo_final = ciclo_mongo

                    context["chiller_status_realtime"] = values.get("estado_chiller", chiller.status)
                    context["chiller_ciclo_realtime"] = ciclo_final
                    context["rendimento"] = values.get("rendimento", 0)

                    context["medidor_values"] = {
                        "PotenciaAtivaTotal_kW": medidor.get("PotenciaAtivaTotal_kW", 0),
                        "Custo": medidor.get("custo", 0),
                        "Energia_consumida_hora": medidor.get("Energia_consumida_hora", 0),
                        "EnergiaAtivaTotal": medidor.get("EnergiaAtivaTotal_output", 0),
                        "EnergiaAtivaParcial": medidor.get("EnergiaAtivaParcial_output", 0),
                        # Campos técnicos para gráficos ou tabelas extra
                        "Media_Corrente": medidor.get("MediaCorrentes_output", 0),
                        "MediaTensoes": medidor.get("MediaTensoes_output", 0),
                    }

                    context["temperaturas"] = values.get("temps", {})
                    context["pressoes"] = values.get("pressoes", {})
            
            except Exception as e:
                print(f"Erro Mongo na View: {e}")

        return context
    
class VariableLogView(LoginRequiredMixin, TemplateView):
    template_name = 'logs.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        layout_context = TemplateLayout.init(self, context)
        context.update(layout_context)

        if not context.get('layout_path'):
            context['layout_path'] = 'base.html'

        # Mesmo critério da view do dashboard, e tem de continuar a ser: as duas páginas
        # partilham o template, portanto uma lista diferente aqui só se notaria como uma
        # variável que aparece a um utilizador e não ao outro. É o tipo do valor que
        # decide o que é desenhável; a lista negra é só para o que é número mas não é uma
        # leitura.
        EXCLUDED_KEYS = {'custo'}

        # -----------------------------------------------------------
        # Capturar cliente autenticado
        # -----------------------------------------------------------
        try:
            cliente = self.request.user.clienteprofile.cliente
        except AttributeError:
            cliente = None

        chiller_id = self.request.GET.get('chiller_id')
        chiller = (
            Chiller.objects.filter(idChiller=chiller_id, idCliente=cliente).first()
            if cliente else None
        )

        if chiller and not chiller.ipcontrolador:
            chiller = None

        context['cliente'] = cliente
        context['chiller'] = chiller

        variaveis = set()

        if chiller:
            try:
                mongo_client = MongoClient(
                    settings.MONGO_URL,
                    serverSelectionTimeoutMS=5000
                )
                # mongo_client.server_info() # Opcional: manter comentado se não quiser validar sempre

                db = mongo_client[settings.MONGO_DB_NAME]
                collection = db["values"]

                # Procura último registo por IP
                last_doc = collection.find_one(
                    {"ip": chiller.ipcontrolador},
                    sort=[("timestamp", -1)]
                )

                if last_doc:
                    values = last_doc.get("values", {}) or {}

                    # As secções não vêm de uma lista fixa: estavam aqui escritas à mão
                    # ("medidor", "temps", "pressoes", "entalpias") e qualquer secção nova
                    # no documento passava despercebida em silêncio.
                    #
                    # O bool é subclasse do int em Python, por isso tem de ser excluído à
                    # mão, senão uma flag entrava na lista como se fosse uma medida.
                    def e_leitura(nome, valor):
                        return (
                            nome not in EXCLUDED_KEYS
                            and isinstance(valor, (int, float))
                            and not isinstance(valor, bool)
                        )

                    for key, content in values.items():
                        if isinstance(content, dict):
                            for sub_key, sub_val in content.items():
                                if e_leitura(sub_key, sub_val):
                                    variaveis.add(sub_key)
                        elif e_leitura(key, content):
                            variaveis.add(key)

            except Exception as e:
                print(f"[VariableLogView Cliente] Erro MongoDB: {e}")
                variaveis = set()

            finally:
                try:
                    mongo_client.close()
                except Exception:
                    pass

        context['variaveis'] = sorted(list(variaveis))
        return context
    
# Toma o cliente do ?cliente_id= da query string, tal como as views do dashboard tomavam:
# um utilizador do cliente A pedia o relatório do B trocando o id.
class RelatorioView(LoginRequiredMixin, ClienteQueryStringMixin, TemplateView):
    template_name = 'relatorio.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        layout_context = TemplateLayout.init(self, context)
        context.update(layout_context)

        if not context.get('layout_path'):
            context['layout_path'] = 'base.html'

        cliente_id = self.request.GET.get('cliente_id')
        chiller_id = self.request.GET.get('chiller_id')
        periodo = self.request.GET.get('periodo', '30')

        cliente = Cliente.objects.filter(idCliente=cliente_id).first()
        chiller = Chiller.objects.filter(idChiller=chiller_id, idCliente=cliente).first()

        context['cliente'] = cliente
        context['chiller'] = chiller

        if chiller and chiller.ipcontrolador:
            try:
                mongo = MongoClient(settings.MONGO_URL, serverSelectionTimeoutMS=2000)
                db = mongo[settings.MONGO_DB_NAME]
                collection = db["values"]

                try:
                    days = int(periodo)
                except (TypeError, ValueError):
                    days = 30

                start_dt = datetime.now() - timedelta(days=days)
                start_str = start_dt.strftime("%Y-%m-%d")

                ip_clean = chiller.ipcontrolador.strip()

                # Carrega todos do IP
                cursor = collection.find({"ip": ip_clean}).sort("timestamp", 1)

                dados_por_dia = {}
                total_consumo = 0.0
                total_custo = 0.0
                cop_total = 0.0
                dias_com_dados = 0

                for doc in cursor:
                    ts = doc.get("timestamp")

                    # Detectar formato
                    if isinstance(ts, str):
                        if ts < start_str:
                            continue
                        data_str = ts[:10]
                    elif isinstance(ts, datetime):
                        if ts < start_dt:
                            continue
                        data_str = ts.strftime("%Y-%m-%d")
                    else:
                        continue

                    valores = doc.get("values", {})
                    medidor = valores.get("medidor", {})

                    # POTÊNCIA (kW)
                    pot = self.safe_float(medidor.get("PotenciaAtivaTotal_kW")
                                          or medidor.get("PotenciaAbsorbida_kW")
                                          or medidor.get("Energia_consumida_hora"))

                    # CUSTO (€/h)
                    custo = self.safe_float(medidor.get("Custo_hora")
                                            or medidor.get("custo"))

                    # COP
                    cop = self.safe_float(valores.get("rendimento") or valores.get("COP"))

                    # Agrupar por dia
                    if data_str not in dados_por_dia:
                        dados_por_dia[data_str] = {
                            "s_pot": 0.0,
                            "s_cus": 0.0,
                            "s_cop": 0.0,
                            "cnt": 0
                        }

                    dados_por_dia[data_str]["s_pot"] += pot
                    dados_por_dia[data_str]["s_cus"] += custo
                    dados_por_dia[data_str]["s_cop"] += cop
                    dados_por_dia[data_str]["cnt"] += 1

                ### Cálculos finais ###
                consumo_diario = []

                for data_str, m in dados_por_dia.items():
                    if m["cnt"] == 0:
                        continue

                    avg_pot = m["s_pot"] / m["cnt"]
                    avg_cus = m["s_cus"] / m["cnt"]
                    avg_cop = m["s_cop"] / m["cnt"]

                    # Energia: kW * 24h
                    kwh_dia = avg_pot * 24

                    # Se custo/h for inválido, estimo com €0,22/kWh
                    if avg_cus < 0.001:
                        custo_dia = kwh_dia * 0.22
                    else:
                        custo_dia = avg_cus * 24

                    consumo_diario.append({
                        "date": data_str,
                        "kwh": round(kwh_dia, 2),
                        "custo": round(custo_dia, 2)
                    })

                    total_consumo += kwh_dia
                    total_custo += custo_dia
                    cop_total += avg_cop
                    dias_com_dados += 1

                media_cop = cop_total / dias_com_dados if dias_com_dados else 0

                insights = [
                    f"Período analisado: {days} dias",
                    f"Consumo total: {total_consumo:.2f} kWh",
                    f"Custo total: € {total_custo:.2f}",
                    f"COP médio: {media_cop:.2f}",
                ]

                context["dados"] = {
                    "total_consumo_kwh": round(total_consumo, 2),
                    "total_custo_eur": round(total_custo, 2),
                    "media_cop": round(media_cop, 2),
                    "media_consumo_diario": round(total_consumo / dias_com_dados, 2) if dias_com_dados else 0,
                    "media_custo_diario": round(total_custo / dias_com_dados, 2) if dias_com_dados else 0,
                    "periodo_dias": days,
                    "insights": insights,
                    "consumo_diario": consumo_diario,
                    "chiller": chiller.nome,
                    "ip": chiller.ipcontrolador,
                    "fluido": chiller.gas,
                    "modelo": chiller.modelo,
                    "n_serie": chiller.n_serie,
                    "data_geracao": datetime.now().strftime("%d/%m/%Y %H:%M"),
                }

                context["periodo_selecionado"] = str(days)

            except Exception as e:
                context["dados"] = {"erro": str(e)}

            finally:
                try:
                    mongo.close()
                except Exception:
                    pass

        else:
            context["dados"] = {}

        # LOGO
        logo_abspath = os.path.abspath('src/assets/img/logo_arcoxxi.png')
        logo_path_str = logo_abspath.replace('\\', '/')
        context['logo_path'] = f"file:///{logo_path_str}" if os.path.exists(logo_abspath) else ""
        return context

    def safe_float(self, val):
        if val is None:
            return 0.0
        try:
            return float(str(val).replace(',', '.'))
        except (TypeError, ValueError):
            return 0.0

    def get(self, request, *args, **kwargs):
        context = self.get_context_data(**kwargs)

        if request.GET.get('pdf') == '1':
            html_string = render_to_string('relatorio_cliente_pdf.html', context)
            pdf_file = weasyprint.HTML(string=html_string).write_pdf()

            response = HttpResponse(pdf_file, content_type='application/pdf')
            response['Content-Disposition'] = (
                f'attachment; filename="relatorio_{datetime.now().strftime("%Y%m%d")}.pdf"'
            )
            return response

        return self.render_to_response(context)


    def get(self, request, *args, **kwargs):
        context = self.get_context_data(**kwargs)

        if request.GET.get('pdf') == '1':
            dados = context.get('dados', {})

            for chave in [
                "chiller", "ip", "fluido", "data_geracao",
                "media_temps", "media_pressoes", "media_medidor", "media_entalpias",
                "total_consumo_kwh", "total_custo_eur", "media_cop",
                "media_consumo_diario", "media_custo_diario",
                "periodo_dias", "insights", "consumo_diario"
            ]:
                if chave not in dados:
                    dados[chave] = "" if "media_" not in chave else {}

            context["dados"] = dados

            # Gera PDF
            html_string = render_to_string('relatorio_cliente_pdf.html', context)
            pdf_file = weasyprint.HTML(string=html_string).write_pdf()
            data_str = datetime.now().strftime('%Y-%m-%d')
            response = HttpResponse(pdf_file, content_type='application/pdf')
            response['Content-Disposition'] = f'attachment; filename="relatorio_{data_str}.pdf"'
            return response

        return self.render_to_response(context)



class ClienteLogoView(LoginRequiredMixin, View):
    """Segunda cópia desta view — a outra está em apps/dashboard/views.py. Nenhuma das duas
    tinha autenticação: o cliente_id é sequencial, portanto qualquer anónimo podia enumerar
    os clientes existentes."""

    def get(self, request, cliente_id):
        if not pode_ver_cliente(request.user, cliente_id):
            raise Http404("Cliente não existe")
        try:
            cliente = Cliente.objects.get(pk=cliente_id)
            if cliente.logo:
                return HttpResponse(cliente.logo, content_type='image/png')
            raise Http404("Logo não encontrada")
        except Cliente.DoesNotExist:
            raise Http404("Cliente não existe")

        
class PerfilView(LoginRequiredMixin, TemplateView):
    template_name = "cliente/profile_user.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        layout_context = TemplateLayout.init(self, context)
        context.update(layout_context)

        # Cliente autenticado
        cliente = self.request.user.clienteprofile.cliente

        context.update({
            "cliente": cliente,
            "layout_path": context.get("layout_path", "base.html")
        })
        return context
    

# =================================================================
#  RELATÓRIOS (Cópia da Lógica Robusta do Admin)
# =================================================================
class ClienteRelatorioView(LoginRequiredMixin, TemplateView):
    template_name = 'relatorio.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(TemplateLayout.init(self, context))
        context.setdefault('layout_path', 'base.html')

        try:
            cliente = self.request.user.clienteprofile.cliente
            chiller_id = self.request.GET.get('chiller_id')
            chiller = Chiller.objects.filter(idChiller=chiller_id, idCliente=cliente).first()
        except Exception as e:
            print(f"[ClienteRelatorioView] Erro a obter cliente/chiller: {e}")
            cliente = None
            chiller = None

        context['cliente'] = cliente
        context['chiller'] = chiller
        periodo = self.request.GET.get('periodo', '30')

        if chiller and chiller.ipcontrolador:
            client = None
            try:
                client = MongoClient(settings.MONGO_URL, serverSelectionTimeoutMS=2000)
                db = client[settings.MONGO_DB_NAME]
                
                try: days = int(periodo)
                except (TypeError, ValueError): days = 30

                start_date = datetime.now() - timedelta(days=days)
                
                # Query Robusta
                cursor = db["values"].find({
                    "ip": chiller.ipcontrolador.strip(),
                    "timestamp": {"$gte": start_date.strftime("%Y-%m-%d")}
                }).sort("timestamp", 1)

                dados_por_dia = {}
                total_kwh = total_eur = total_cop = 0.0
                dias_validos = 0

                for doc in cursor:
                    # Data (Agrupamento)
                    ts = doc.get("timestamp", "")
                    if len(ts) >= 10: data_str = ts[:10]
                    else: continue

                    # Valores
                    vals = doc.get("values", {})
                    med = vals.get("medidor", {})
                    
                    # Extração Segura
                    pot = safe_float(med.get("PotenciaAtivaTotal_kW") or med.get("PotenciaAbsorbida_kW"))
                    cus = safe_float(med.get("custo") or med.get("Custo_hora"))
                    cop = safe_float(vals.get("rendimento") or vals.get("COP"))

                    if data_str not in dados_por_dia:
                        dados_por_dia[data_str] = {'s_p':0,'s_c':0,'s_cop':0,'n':0}
                    
                    d = dados_por_dia[data_str]
                    d['s_p']+=pot; d['s_c']+=cus; d['s_cop']+=cop; d['n']+=1

                # Totais
                consumo_diario = []
                for d, m in dados_por_dia.items():
                    if m['n'] == 0: continue
                    avg_p = m['s_p']/m['n']
                    avg_c = m['s_c']/m['n']
                    
                    kwh = avg_p * 24
                    eur = (kwh * 0.22) if avg_c < 0.001 else (avg_c * 24)
                    
                    consumo_diario.append({'date':d, 'kwh':round(kwh,2), 'custo':round(eur,2)})
                    total_kwh += kwh
                    total_eur += eur
                    total_cop += (m['s_cop']/m['n'])
                    dias_validos += 1

                avg_cop = total_cop/dias_validos if dias_validos else 0
                
                context['dados'] = {
                    "total_consumo_kwh": total_kwh,
                    "total_custo_eur": total_eur,
                    "media_cop": avg_cop,
                    "consumo_diario": consumo_diario,
                    "periodo_dias": days,
                    "data_geracao": datetime.now().strftime("%d/%m/%Y"),
                    "insights": [f"Dias analisados: {dias_validos}", f"Consumo Total: {total_kwh:.2f} kWh"]
                }
                context['periodo_selecionado'] = str(days)

            except Exception as e:
                print(f"Erro Relatório Cliente: {e}")
                context['dados'] = {}
            finally:
                if client: client.close()
        else:
            context['dados'] = {}

        return context

    def get(self, request, *args, **kwargs):
        context = self.get_context_data(**kwargs)
        if request.GET.get('pdf') == '1':
            html = render_to_string('relatorio_cliente_pdf.html', context)
            pdf = weasyprint.HTML(string=html).write_pdf()
            response = HttpResponse(pdf, content_type='application/pdf')
            response['Content-Disposition'] = f'attachment; filename="Relatorio_Cliente.pdf"'
            return response
        return self.render_to_response(context)
    
class ClienteLivroObraView(LoginRequiredMixin, TemplateView):
    template_name = 'maintenance_log_cliente.html' 

    def get_context_data(self, **kwargs):
        # 1. Inicializar Layout
        context = super().get_context_data(**kwargs)
        layout_context = TemplateLayout.init(self, context)
        context.update(layout_context)
        
        if not context.get('layout_path'): 
            context['layout_path'] = 'base.html'

        # 2. Obter Cliente Logado
        try:
            # Ajusta conforme o teu modelo de perfil (Related_name)
            cliente = self.request.user.clienteprofile.cliente
        except AttributeError:
            cliente = None
        
        # 3. Filtrar Chillers
        chillers = []
        if cliente:
            chillers = Chiller.objects.filter(idCliente=cliente)

        # 4. Obter Chiller Selecionado (Com Auto-Select)
        chiller_id = self.request.GET.get('chiller_id')
        chiller = None
        
        if chillers.exists():
            if chiller_id:
                chiller = chillers.filter(idChiller=chiller_id).first()
            
            # NOVO: Se não houver ID na URL, seleciona o primeiro automaticamente
            if not chiller:
                chiller = chillers.first()

        # 5. Dados SQL (Logs e Horas)
        logs = []
        horas_atuais = 0.0
        progresso_percent = 0
        
        if chiller:
            logs = Intervencao.objects.filter(chiller=chiller).order_by('-data_intervencao')
            try:
                horas_atuais = float(chiller.horas_funcionamento or 0.0)
            except (ValueError, TypeError):
                horas_atuais = 0.0

            if horas_atuais > 0:
                progresso_percent = (horas_atuais / 10000.0) * 100

        # 6. Lógica de Status Inteligente (CORRIGIDA)
        # O padrão começa como 'offline', mas vamos tentar provar que está ligado
        status_display = 'offline'
        mongo_ctx = {'rendimento': 0, 'medidor': {}}
        
        if chiller:
            # A. Limpeza e Normalização do Status SQL
            # Remove espaços extras e converte para minúsculas
            raw_status = str(chiller.status).strip().lower() if chiller.status else ''

            # B. Mapeamento SQL -> Visual
            if raw_status in ['ligado', 'ativo', 'on', 'start', 'funcionamento']:
                status_display = 'ativo'  # Fica VERDE
            
            elif raw_status in ['desativado', 'off', 'stop', 'parado']:
                status_display = 'desativado' # Fica VERMELHO
            
            elif 'manuten' in raw_status:
                status_display = 'manutencao' # Fica AZUL
            
            else:
                status_display = raw_status # Outro valor qualquer

            # C. Verificar MongoDB (Apenas se SQL estiver 'ativo')
            if status_display == 'ativo' and chiller.ipcontrolador:
                try:
                    mongo_data = get_mongo_data(chiller.ipcontrolador)
                    if mongo_data:
                        mongo_ctx.update(mongo_data)
                        
                        # Verifica se o Mongo contradiz o SQL explicitamente
                        m_status = mongo_data.get('status', '').lower()
                        if m_status in ['desativado', 'off', 'alarm', 'offline']:
                            status_display = m_status
                except Exception:
                    # Se der erro (timeout, etc), ignoramos e confiamos no SQL "Ligado"
                    pass

        # 7. Atualizar Contexto
        context.update({
            'cliente': cliente,
            'chiller': chiller,
            'chillers': chillers,
            'logs': logs,
            'horas_atuais': horas_atuais,
            'horas_limite': 10000.0,
            'progresso_percent': progresso_percent,
            'readonly': True,
            
            # Variável crucial corrigida
            'chiller_status_realtime': status_display,
            
            'rendimento': mongo_ctx.get('rendimento'),
            'medidor_values': mongo_ctx.get('medidor')
        })
        
        return context
