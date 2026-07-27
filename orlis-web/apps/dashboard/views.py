import json
import os
import logging
from datetime import datetime, timedelta

# Django Standard Imports
from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse, Http404
from django.views import View
from django.views.generic import TemplateView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.template.loader import render_to_string
from django.db.models import ProtectedError
from django.urls import reverse
from web_project import TemplateLayout
from django.contrib import messages #para os toast pop ups
from django.contrib.auth.models import User

# DRF Imports
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

# MongoDB Imports
from pymongo import MongoClient, DESCENDING
from pymongo.errors import ConnectionFailure

# Third Party Imports
import weasyprint
from weasyprint import HTML

# Project Imports
from web_project import TemplateLayout
from .models import Cliente, Chiller, ClienteChillers, Intervencao


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = 'dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        layout_context = TemplateLayout.init(self, context)
        context.update(layout_context)
        if not context.get('layout_path'):
            context['layout_path'] = 'base.html'

        clientes = Cliente.objects.all().order_by('nome')
        cliente_id = self.request.GET.get('cliente_id')
        chillers = []
        cliente_selecionado = None

        if cliente_id:
            cliente_selecionado = Cliente.objects.filter(idCliente=cliente_id).first()
            if cliente_selecionado:
                chillers = Chiller.objects.filter(idCliente=cliente_selecionado).order_by('nome')
        else:
            chillers = []

        context.update({
            'clientes': clientes,
            'cliente_selecionado': cliente_selecionado,
            'chillers': chillers
        })
        return context

    def post(self, request, *args, **kwargs):
        cliente_id = request.POST.get('cliente_id_hidden') or request.GET.get('cliente_id')

        # --- AÇÃO: APAGAR ---
        if 'delete_id' in request.POST:
            chiller_id = request.POST['delete_id']
            chiller = get_object_or_404(Chiller, idChiller=chiller_id)
            redirect_cliente_id = chiller.idCliente.idCliente 
            chiller.delete()
            
            # MENSAGEM DE SUCESSO
            messages.success(request, "Chiller apagado com sucesso!") 
            return redirect(f"{reverse('dashboard')}?cliente_id={redirect_cliente_id}")

        # --- AÇÃO: EDITAR ---
        elif 'edit_id' in request.POST:
            chiller = get_object_or_404(Chiller, idChiller=request.POST['edit_id'])
            
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
            
            # MENSAGEM DE SUCESSO
            messages.success(request, f"Chiller '{chiller.nome}' atualizado com sucesso!")
            return redirect(f"{reverse('dashboard')}?cliente_id={chiller.idCliente.idCliente}")

        # --- AÇÃO: CRIAR NOVO ---
        if not cliente_id:
             messages.error(request, "Erro: Nenhum cliente selecionado.")
             return redirect(reverse('dashboard'))

        cliente = get_object_or_404(Cliente, idCliente=cliente_id)

        nome = request.POST.get('nome')
        localizacao = request.POST.get('localizacao')
        ipcontrolador = request.POST.get('ipcontrolador')

        if nome and localizacao and ipcontrolador:
            lat = request.POST.get('latitude')
            lng = request.POST.get('longitude')
            
            Chiller.objects.create(
                idCliente=cliente,
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
                longitude=float(lng) if lng else None
            )
            # MENSAGEM DE SUCESSO
            messages.success(request, "Novo chiller registado com sucesso!")
        else:
            messages.error(request, "Erro: Preencha os campos obrigatórios.")

        return redirect(f"{reverse('dashboard')}?cliente_id={cliente_id}")

class SensorDataAPIView(APIView):
    # Esta API é consumida tanto pelo dashboard interno como pela área de cliente
    # (page_2_cliente.html, logs.html), por isso não basta exigir login: o parâmetro
    # "ip" é escolhido por quem chama, e sem verificação de posse um cliente
    # autenticado leria a telemetria dos chillers de outro cliente.
    permission_classes = [IsAuthenticated]

    @staticmethod
    def _pode_ver_chiller(user, ip_address):
        """Um superutilizador vê qualquer chiller; um utilizador de cliente só vê os
        do cliente a que está associado. Sem associação, não vê nenhum."""
        if user.is_superuser:
            return True
        cliente = getattr(getattr(user, 'clienteprofile', None), 'cliente', None)
        if cliente is None:
            return False
        return Chiller.objects.filter(ipcontrolador=ip_address, idCliente=cliente).exists()

    def get(self, request):
        # 1. Obter o IP a partir dos parâmetros da URL (?ip=...)
        ip_address = request.GET.get('ip')

        # 2. Validar se o IP foi fornecido
        if not ip_address:
            return Response(
                {"detail": "Parâmetro 'ip' é obrigatório."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 2b. Validar que este utilizador tem direito a este chiller. Responde 404 e não
        # 403 de propósito: um 403 confirmaria a existência do IP a quem não lhe pertence.
        if not self._pode_ver_chiller(request.user, ip_address):
            return Response(
                {"detail": "Chiller não encontrado."},
                status=status.HTTP_404_NOT_FOUND
            )

        client = None
        try:
            # 3. Conectar ao MongoDB
            client = MongoClient(settings.MONGO_URL, serverSelectionTimeoutMS=5000)
            # client.server_info()  # Opcional em produção para performance
            db = client[settings.MONGO_DB_NAME]
            values_collection = db["values"]

            # 4. Criar a query
            query = {"ip": ip_address}

            # 5. Executar a query filtrada e ordenada (Limit 60 para coincidir com o histórico do JS)
            # Ordena por "recebido_em" (hora de receção no servidor) e não por "timestamp"
            # (relógio do chiller): um chiller com o relógio adiantado ficaria cravado no
            # topo e o dashboard mostraria essa leitura como atual, ignorando tudo o que
            # chegasse depois. O "_id" desempata leituras recebidas no mesmo segundo.
            # Documentos antigos, gravados antes deste campo existir, não têm
            # "recebido_em": no Mongo ordenam como null, portanto ficam depois dos
            # recentes, que é o que se quer, e entre si desempatam pelo "_id".
            values_docs = list(
                values_collection.find(query)
                .sort([("recebido_em", -1), ("_id", -1)])
                .limit(60)
            )

        except ConnectionFailure as e:
            return Response(
                {"detail": f"Erro de conexão MongoDB: {e}"}, 
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        except Exception as e:
            return Response(
                {"detail": f"Erro inesperado na base de dados: {e}"}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        finally:
            if client:
                client.close()

        # 6. Processar os resultados com FLATTENING DINÂMICO
        processed_data = []
        custo_recente = 0

        if values_docs:
            # Tenta pegar o custo do documento mais recente para o card de resumo (se existir)
            latest_vals = values_docs[0].get("values", {})
            if "medidor" in latest_vals and isinstance(latest_vals["medidor"], dict):
                custo_recente = latest_vals["medidor"].get("custo_hora", 0)

            for doc in values_docs:
                raw_values = doc.get("values", {})
                
                # Dados Base
                entry = {
                    "chiller": doc.get("chiller", ""),
                    "fluido": doc.get("fluido", ""),
                    "IP": doc.get("ip", ""),
                    "timestamp": doc.get("timestamp", datetime.now().isoformat()),
                    # Hora a que o servidor recebeu a leitura. É por esta que a lista vem
                    # ordenada, por isso tem de ir para o frontend: sem ela, uma leitura
                    # com o relógio adiantado aparece no topo a mostrar uma hora futura,
                    # sem nada que denuncie a discrepância. Vem a null nos documentos
                    # anteriores à introdução do campo.
                    "recebido_em": doc.get("recebido_em"),
                    "aviso": raw_values.get("aviso", "") # O aviso costuma estar na raiz de values
                }

                # --- LÓGICA DINÂMICA (A Mágica acontece aqui) ---
                # Varre todos os itens dentro de 'values'. 
                # Se for dict (ex: medidor), extrai os filhos. Se for valor (ex: rendimento), usa direto.
                
                for key, val in raw_values.items():
                    if isinstance(val, dict):
                        # É uma categoria (medidor, temps, pressoes, entalpias)
                        # Copia tudo o que está dentro para a raiz do objeto 'entry'
                        for sub_key, sub_val in val.items():
                            entry[sub_key] = sub_val 
                    elif key not in entry: 
                        # É um valor solto na raiz (ex: rendimento, COP) e ainda não existe em entry
                        entry[key] = val
                
                # Nota: Com isto, entry['custo'] passará a existir automaticamente
                processed_data.append(entry)

        # 7. Retorno
        response_data = {
            "sensor_data": processed_data,
            "ip_address": ip_address,
            "custo_hora": custo_recente, 
        }

        if not processed_data:
             return Response(
                {"detail": "Nenhum dado encontrado para este IP.", "sensor_data": []}, 
                status=status.HTTP_404_NOT_FOUND
            )

        return Response(response_data)

# Configuração de Logs
logger = logging.getLogger(__name__)

class ClienteLogoView(View):
    def get(self, request, cliente_id):
        try:
            cliente = Cliente.objects.get(pk=cliente_id)
            if cliente.logo:
                return HttpResponse(cliente.logo, content_type='image/png')
            raise Http404("Logo não encontrada")
        except Cliente.DoesNotExist:
            raise Http404("Cliente não existe")

# Conexão global MongoDB (instanciada fora da classe para performance)
try:
    mongo_client = MongoClient(settings.MONGO_URL, serverSelectionTimeoutMS=2000)
    db_mongo = mongo_client[settings.MONGO_DB_NAME]
except Exception:
    db_mongo = None

class ChillerDetailView(LoginRequiredMixin, TemplateView):
    template_name = 'page_2.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        layout_context = TemplateLayout.init(self, context)
        context.update(layout_context)

        if not context.get('layout_path'):
            context['layout_path'] = 'base.html'

        cliente_id = self.request.GET.get('cliente_id')
        chiller_id = self.request.GET.get('chiller_id')

        cliente = None
        chillers = []
        chiller = None

        if cliente_id:
            cliente = Cliente.objects.filter(idCliente=cliente_id).first()
            if cliente:
                chillers = Chiller.objects.filter(idCliente=cliente)
                if chiller_id:
                    chiller = chillers.filter(idChiller=chiller_id).first()

        context['cliente'] = cliente
        context['chillers'] = chillers
        context['chiller'] = chiller

        # Fallback inicial
        context.update({
            "chiller_status_realtime": chiller.status if chiller else "N/A",
            "chiller_ciclo_realtime": chiller.ciclo if chiller and hasattr(chiller, "ciclo") else "N/A",
            "rendimento": 0,
            "medidor_values": {},
            "temperaturas": {},
            "pressoes": {},
            "entalpias": {},
            "ip_address": "N/A",
            "fluido": chiller.gas if chiller else "N/A"
        })

        # Busca dados em tempo real do MongoDB
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

                    # Ciclo com fallback (como no ClienteChillerDetailView)
                    ciclo_mongo = values.get("ciclo")
                    if ciclo_mongo in [None, "", "null"]:
                        ciclo_final = chiller.ciclo or "N/A"
                    else:
                        ciclo_final = ciclo_mongo

                    context.update({
                        "chiller_status_realtime": values.get("estado_chiller", chiller.status),
                        "chiller_ciclo_realtime": ciclo_final,
                        "rendimento": values.get("rendimento", 0),
                        "temperaturas": values.get("temps", {}),
                        "pressoes": values.get("pressoes", {}),
                        "entalpias": values.get("entalpias", {}),
                        "ip_address": last_doc.get("ip", "N/A"),
                        "fluido": chiller.gas,
                        "medidor_values": {
                            "Corrente_L1": medidor.get("Corrente_L1_output", 0),
                            "Corrente_L2": medidor.get("Corrente_L2_output", 0),
                            "Corrente_L3": medidor.get("Corrente_L3_output", 0),
                            "Media_Corrente": medidor.get("MediaCorrentes_output", 0),
                            "PotenciaAbsorbida_kW": medidor.get("PotenciaAbsorbida_kW", 0),
                            "EnergiaAtivaParcial": medidor.get("EnergiaAtivaParcial_output", 0),
                            "EnergiaReativaParcial": medidor.get("EnergiaReativaParcial_output", 0),
                            "EnergiaAtivaTotal": medidor.get("EnergiaAtivaTotal_output", 0),
                            "EnergiaReativaTotal": medidor.get("EnergiaReativaTotal_output", 0),
                            "Tensao_L1_L2": medidor.get("Tensao_L1_L2_output", 0),
                            "Tensao_L2_L3": medidor.get("Tensao_L2_L3_output", 0),
                            "Tensao_L3_L1": medidor.get("Tensao_L3_L1_output", 0),
                            "MediaTensoes": medidor.get("MediaTensoes_output", 0),
                            "Consumo_hora": medidor.get("Energia_consumida_hora", 0),
                            "Custo": medidor.get("custo", 0),
                            "PotenciaAtivaTotal_kW": medidor.get("PotenciaAtivaTotal_kW", 0),
                            "estado_chiller": values.get("estado_chiller", chiller.status),
                            "ciclo": ciclo_final
                        }
                    })

            except Exception as e:
                logger.error(f"Erro ao aceder ao MongoDB (Page 2): {e}")

        return context

class VariableLogView(LoginRequiredMixin, TemplateView):
    template_name = 'page_5.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        layout_context = TemplateLayout.init(self, context)
        context.update(layout_context)

        if not context.get('layout_path'):
            context['layout_path'] = 'base.html'

        cliente_id = self.request.GET.get('cliente_id')
        chiller_id = self.request.GET.get('chiller_id')

        cliente = Cliente.objects.filter(idCliente=cliente_id).first() if cliente_id else None
        chiller = Chiller.objects.filter(idChiller=chiller_id, idCliente=cliente).first() if cliente and chiller_id else None

        if chiller and not chiller.ipcontrolador:
            chiller = None

        context['cliente'] = cliente
        context['chiller'] = chiller
        context['has_ip'] = bool(chiller and chiller.ipcontrolador)

        variaveis = []

        # --- LISTA NEGRA (EXCLUSÕES) ---
        # Atualizado para incluir versões com underscore (_) conforme sua imagem
        EXCLUDED_VARS = {
            'custo', 
            'estado_chiller', 'estado chiller',  # Cobre ambas as possibilidades
            'estado_h1', 'estado h1',
            'estado_h2', 'estado h2',
            'h1', 'h2', 'h3', 'h4',
            'aviso'
        }

        if chiller:
            try:
                mongo_client = MongoClient(settings.MONGO_URL, serverSelectionTimeoutMS=5000)
                db = mongo_client[settings.MONGO_DB_NAME]
                collection = db["values"]

                # 1) Procurar por IP
                query = {"ip": chiller.ipcontrolador}
                last_doc = collection.find_one(query, sort=[("timestamp", -1)])

                # 2) Fallback por nome
                if not last_doc:
                    query = {"chiller": chiller.nome}
                    last_doc = collection.find_one(query, sort=[("timestamp", -1)])

                if last_doc:
                    values = last_doc.get("values", {}) or {}
                    found_keys = set()

                    for key, content in values.items():
                        # CASO A: Grupo (dicionário aninhado)
                        if isinstance(content, dict):
                            for sub_key in content.keys():
                                if sub_key not in EXCLUDED_VARS:
                                    found_keys.add(sub_key)
                            
                        # CASO B: Valor direto
                        elif isinstance(content, (int, float, str)):
                            if key not in EXCLUDED_VARS:
                                found_keys.add(key)
                    
                    variaveis = sorted(list(found_keys))
                else:
                    variaveis = []

            except Exception as e:
                print(f"Erro ao buscar variáveis no Mongo: {e}")
                variaveis = []

            finally:
                try:
                    mongo_client.close()
                except Exception:
                    pass

        context['variaveis'] = variaveis
        return context

class TableClientsView(LoginRequiredMixin, TemplateView):
    template_name = 'table_clients.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        layout_context = TemplateLayout.init(self, context)
        context.update(layout_context)

        if not context.get('layout_path'):
            context['layout_path'] = 'base.html'

        # Adicionado order_by para consistência na tabela
        context['clientes'] = ClienteChillers.objects.all().order_by('nome') 
        return context
    
class ClienteChillerDetailView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        cliente_id = request.GET.get("id")
        cliente = get_object_or_404(ClienteChillers, idCliente=cliente_id)

        chillers_data = []
        for chiller in cliente.chillers.all():
            chillers_data.append({
                "nome": chiller.nome,
                "marca": chiller.marca,
                "modelo": chiller.modelo,
                "ipcontrolador": chiller.ipcontrolador
            })

        return JsonResponse({
            "nome": cliente.nome,
            "email": cliente.email,
            "chillers": chillers_data
        })
    
class ClienteUpdateView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        try:
            # IMPORTANTE: Usamos request.POST e request.FILES para suportar upload de arquivos (FormData)
            # Não use json.loads aqui.
            
            cliente_id = request.POST.get('cliente_id') # Nome deve bater com o <input name="cliente_id">
            cliente = get_object_or_404(ClienteChillers, idCliente=cliente_id)

            # Atualiza campos de texto
            cliente.nome = request.POST.get('nome')
            cliente.email = request.POST.get('email')
            cliente.telefone = request.POST.get('telefone')
            cliente.localidade = request.POST.get('localidade')
            cliente.nif = request.POST.get('nif')

            # Atualiza Logo se houver arquivo novo
            if 'logo' in request.FILES:
                cliente.logo = request.FILES['logo']

            cliente.save()

            # Gera a URL para atualizar o preview no frontend imediatamente
            # Ajuste 'cliente-logo' para o nome exato da sua url no urls.py
            new_logo_url = reverse('cliente-logo', args=[cliente.pk]) if cliente.logo else None

            return JsonResponse({
                'status': 'success', 
                'message': 'Cliente atualizado.',
                'new_logo_url': new_logo_url
            })
            
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
        

class ClienteDeleteView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        try:
            data = json.loads(request.body)
            cliente_id = data.get('id')

            # 1. Encontrar o Cliente
            cliente = get_object_or_404(ClienteChillers, idCliente=cliente_id)

            # 2. Procurar e Apagar o User associado
            # A lógica é: Encontrar o Profile ligado a este cliente -> Apanhar o User desse Profile -> Apagar o User
            try:
                # Tenta encontrar o perfil. O nome do campo na DB é 'cliente_id', 
                # então no Django deve ser filter(cliente=cliente)
                
                from django.apps import apps
                
                try:
                    ProfileModel = apps.get_model('auth', 'Profile') 
                except LookupError:
                    ProfileModel = apps.get_model('cliente', 'ClienteProfile') # Exemplo

                # Procura o perfil
                perfil = ProfileModel.objects.filter(cliente=cliente).first()

                if perfil and perfil.user:
                    user_id_para_apagar = perfil.user.id
                    user = User.objects.get(id=user_id_para_apagar)
                    
                    # AO APAGAR O USER, O DJANGO APAGA O PERFIL AUTOMATICAMENTE (CASCADE)
                    user.delete()
                    print(f"Utilizador (ID {user_id_para_apagar}) apagado com sucesso.")
                else:
                    print("Nenhum utilizador encontrado para este cliente.")

            except Exception as e:
                print(f"Aviso: Não foi possível apagar o utilizador associado: {str(e)}")

            # 3. Apagar o Cliente
            if ClienteChillers.objects.filter(idCliente=cliente_id).exists():
                cliente.delete()

            return JsonResponse({'status': 'success', 'message': 'Cliente e conta de utilizador removidos.'})

        except ProtectedError:
            return JsonResponse({
                'status': 'error', 
                'message': 'Não é possível remover: Existem Chillers ou registos dependentes deste cliente.'
            }, status=400)

        except ClienteChillers.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': 'Cliente já não existe.'}, status=404)

        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
        
class SuperuserRequiredMixin(UserPassesTestMixin):
    """Restringe uma view aos utilizadores internos. O login já encaminha os
    superutilizadores para /dashboard/ e os restantes para /cliente/ (ver
    auth/login/views.py: redirect_by_role), mas as views de escrita abaixo são
    chamadas por POST a partir de JavaScript e nada impediria um utilizador de
    cliente autenticado de lhes chamar diretamente."""

    def test_func(self):
        return self.request.user.is_superuser


class ChillerUpdateView(SuperuserRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        try:
            # Carrega os dados JSON enviados pelo JavaScript
            data = json.loads(request.body)
            # Obtém o ID do chiller a partir dos dados
            chiller_id = data.get('id') 

            # Encontra o chiller na base de dados
            chiller = get_object_or_404(Chiller, idChiller=chiller_id)

            # Atualiza os campos de texto padrão
            chiller.nome = data.get('nome', chiller.nome)
            chiller.localizacao = data.get('localizacao', chiller.localizacao)
            chiller.ipcontrolador = data.get('ipcontrolador', chiller.ipcontrolador)
            chiller.marca = data.get('marca', chiller.marca)
            chiller.modelo = data.get('modelo', chiller.modelo)
            chiller.n_serie = data.get('n_serie', chiller.n_serie)
            chiller.gas = data.get('gas', chiller.gas)
            chiller.ciclo = data.get('ciclo', chiller.ciclo)
            chiller.status = data.get('status', chiller.status)
            
            lat = data.get('latitude')
            if lat is not None and lat != "":
                chiller.latitude = lat
            else:
                chiller.latitude = None

            lng = data.get('longitude')
            if lng is not None and lng != "":
                chiller.longitude = lng
            else:
                chiller.longitude = None
            
            # Guarda as alterações na base de dados
            chiller.save()
            
            return JsonResponse({'status': 'success', 'message': 'Chiller atualizado com sucesso'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

class ChillerDeleteView(SuperuserRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        try:
            data = json.loads(request.body)
            chiller_id = data.get('id')

            chiller = get_object_or_404(Chiller, idChiller=chiller_id)
            chiller.delete() 

            return JsonResponse({'status': 'success', 'message': 'Chiller apagado com sucesso'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
        
class RelatorioView(LoginRequiredMixin, TemplateView):
    template_name = 'relatorios.html'

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
            client = None
            try:
                client = MongoClient(settings.MONGO_URL, serverSelectionTimeoutMS=2000)
                db = client[settings.MONGO_DB_NAME]
                collection = db["values"]

                try: days = int(periodo)
                except (TypeError, ValueError): days = 30

                # Data de corte
                start_date_dt = datetime.now() - timedelta(days=days)
                start_date_str = start_date_dt.strftime("%Y-%m-%d")

                # Limpeza do IP
                ip_clean = chiller.ipcontrolador.strip()

                # QUERY ROBUSTA: Trazemos tudo deste IP e filtramos a data no Python
                # Isto resolve o problema de incompatibilidade entre String/Date no Mongo
                cursor = collection.find({"ip": ip_clean}).sort("timestamp", 1)

                dados_por_dia = {}
                total_consumo = 0.0
                total_custo = 0.0
                cop_sum_total = 0.0
                dias_com_dados = 0

                for doc in cursor:
                    # 1. Filtro de Data Manual (Mais seguro)
                    timestamp = doc.get("timestamp")
                    data_str = ""
                    
                    if isinstance(timestamp, str):
                        # Se for string "2025-11-30..."
                        if timestamp < start_date_str: continue 
                        data_str = timestamp[:10]
                    elif isinstance(timestamp, datetime):
                        # Se for objeto Date
                        if timestamp < start_date_dt: continue
                        data_str = timestamp.strftime("%Y-%m-%d")
                    else:
                        continue

                    # 2. Extração de Valores
                    vals = doc.get("values", {})
                    medidor = vals.get("medidor", {})

                    # Tenta ler a potência (kW)
                    # Usa PotenciaAtivaTotal_kW (17.74 no seu JSON)
                    raw_p = medidor.get("PotenciaAtivaTotal_kW") or medidor.get("PotenciaAbsorbida_kW")
                    potencia_kw = self.safe_float(raw_p)

                    # Tenta ler o custo (€/h)
                    raw_c = medidor.get("Custo_hora") or medidor.get("custo")
                    custo_hora = self.safe_float(raw_c)
                    
                    # Tenta ler o COP
                    cop = self.safe_float(vals.get("rendimento") or vals.get("COP"))

                    # 3. Agregação
                    if data_str not in dados_por_dia:
                        dados_por_dia[data_str] = {'s_pot': 0.0, 's_cus': 0.0, 's_cop': 0.0, 'cnt': 0}

                    dados_por_dia[data_str]['s_pot'] += potencia_kw
                    dados_por_dia[data_str]['s_cus'] += custo_hora
                    dados_por_dia[data_str]['s_cop'] += cop
                    dados_por_dia[data_str]['cnt'] += 1

                # 4. Médias Finais
                consumo_diario = []
                for data_str, m in dados_por_dia.items():
                    if m['cnt'] == 0: continue
                    
                    avg_pot = m['s_pot'] / m['cnt']
                    avg_cus = m['s_cus'] / m['cnt']
                    avg_cop = m['s_cop'] / m['cnt']

                    # CÁLCULO: Potência Média (kW) * 24 Horas = Energia (kWh)
                    kwh_dia = avg_pot * 24
                    
                    # Custo: Se o sensor der ~0, estimamos com 0.22€/kWh
                    if avg_cus < 0.001: cust_dia = kwh_dia * 0.22
                    else: cust_dia = avg_cus * 24

                    consumo_diario.append({
                        "date": data_str,
                        "kwh": round(kwh_dia, 2),
                        "custo": round(cust_dia, 2)
                    })

                    total_consumo += kwh_dia
                    total_custo += cust_dia
                    cop_sum_total += avg_cop
                    dias_com_dados += 1

                media_cop = cop_sum_total / dias_com_dados if dias_com_dados else 0
                
                # Insights
                insights = [
                    f"Período: {dias_com_dados} dias analisados",
                    f"Consumo Total: {total_consumo:.2f} kWh",
                    f"Custo Total: € {total_custo:.2f}",
                    f"COP Médio: {media_cop:.2f}",
                ]

                context['dados'] = {
                    "total_consumo_kwh": total_consumo,
                    "total_custo_eur": total_custo,
                    "media_cop": media_cop,
                    "media_consumo_diario": (total_consumo/dias_com_dados if dias_com_dados else 0),
                    "media_custo_diario": (total_custo/dias_com_dados if dias_com_dados else 0),
                    "periodo_dias": days,
                    "insights": insights,
                    "consumo_diario": consumo_diario,
                    "chiller": chiller.nome,
                    "cliente": cliente.nome,
                    "ip": chiller.ipcontrolador,
                    "modelo": chiller.modelo,
                    "n_serie": chiller.n_serie,
                    "fluido": chiller.gas,
                    "data_geracao": datetime.now().strftime("%d/%m/%Y %H:%M"),
                }
                context['periodo_selecionado'] = str(days)

            except Exception:
                context['dados'] = {}
            finally:
                if client: client.close()
        else:
            context['dados'] = {}

        logo_abspath = os.path.abspath('src/assets/img/logo_arcoxxi.png')
        logo_path_str = logo_abspath.replace('\\', '/')
        context['logo_path'] = f"file:///{logo_path_str}" if os.path.exists(logo_abspath) else ""
        return context

    def safe_float(self, val):
        if val is None or val == "": return 0.0
        try: return float(str(val).replace(',', '.'))
        except (TypeError, ValueError): return 0.0

    def get(self, request, *args, **kwargs):
        context = self.get_context_data(**kwargs)
        if request.GET.get('pdf') == '1':
            html_string = render_to_string('relatorio_pdf.html', context)
            pdf_file = weasyprint.HTML(string=html_string).write_pdf()
            filename = f"Relatorio_{datetime.now().strftime('%Y%m%d')}.pdf"
            response = HttpResponse(pdf_file, content_type='application/pdf')
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response
        return self.render_to_response(context)


# --- Helper Mongo (Usado no Livro de Obra) ---
logger = logging.getLogger(__name__)
def get_mongo_data(ip_chiller):
    data = {
        'status': 'offline', 'ciclo': 'N/A', 'rendimento': None, 
        'medidor': {'Custo': 0.0, 'PotenciaAtivaTotal_kW': 0.0}
    }
    if not ip_chiller: return data
    try:
        client = MongoClient(settings.MONGO_URL, serverSelectionTimeoutMS=2000)
        db = client[settings.MONGO_DB_NAME]
        collection = db["values"]
        last_doc = collection.find_one({"ip": ip_chiller}, sort=[('_id', DESCENDING)])
        if last_doc and 'values' in last_doc:
            vals = last_doc['values']
            medidor = vals.get('medidor', {})
            data['status'] = medidor.get('estado_chiller', 'offline')
            data['ciclo'] = 'Arrefecimento'
            data['rendimento'] = vals.get('rendimento')
            data['medidor']['Custo'] = medidor.get('custo_hora', 0.0)
            data['medidor']['PotenciaAtivaTotal_kW'] = medidor.get('PotenciaAbsorbida_kW', 0.0)
        client.close()
    except Exception as e:
        logger.error(f"Erro Mongo Helper: {e}")
    return data

class LivroObraView(LoginRequiredMixin, TemplateView):
    template_name = 'maintenance_log.html'

    def get_context_data(self, **kwargs):
        # 1. Inicializar Layout
        context = super().get_context_data(**kwargs)
        layout_context = TemplateLayout.init(self, context)
        context.update(layout_context)
        if not context.get('layout_path'): 
            context['layout_path'] = 'base.html'

        # 2. Obter IDs da URL
        cliente_id = self.request.GET.get('cliente_id')
        chiller_id = self.request.GET.get('chiller_id')
        
        cliente = None
        chiller = None
        chillers = []

        # 3. Lógica Principal
        if cliente_id:
            # Obter Cliente
            cliente = get_object_or_404(Cliente, idCliente=cliente_id)
            
            # Obter Lista de Chillers para o Dropdown
            chillers = Chiller.objects.filter(idCliente=cliente)

            # Lógica de Seleção do Chiller Ativo
            if chillers.exists():
                if chiller_id:
                    # Tenta pegar o específico
                    chiller = chillers.filter(idChiller=chiller_id).first()
                
                # SE NÃO HOUVER CHILLER SELECIONADO (ou ID inválido), PEGA O PRIMEIRO
                if not chiller:
                    chiller = chillers.first()

        # 4. Dados SQL (Logs e Horas)
        logs = []
        horas_atuais = 0.0
        progresso_percent = 0
        
        if chiller:
            logs = Intervencao.objects.filter(chiller=chiller).order_by('-data_intervencao')
            try:
                horas_atuais = float(chiller.horas_funcionamento or 0.0)
            except (TypeError, ValueError):
                horas_atuais = 0.0
                
            if horas_atuais > 0:
                progresso_percent = (horas_atuais / 10000.0) * 100

        status_display = 'offline' 
        mongo_ctx = {'rendimento': 0, 'medidor': {}}

        if chiller:
            # PASSO A: Ler o status do SQL e limpar a string
            # .strip() remove espaços vazios antes e depois
            # .lower() transforma "Ligado" em "ligado"
            raw_status = str(chiller.status).strip().lower() if chiller.status else ''
            
            # DEBUG: Vai aparecer no teu terminal onde corre o servidor
            print(f"--- DEBUG STATUS --- ID: {chiller.idChiller} | SQL Original: '{chiller.status}' | Formatado: '{raw_status}'")

            # PASSO B: Mapeamento SQL -> Variáveis do Template
            # Verifica várias possibilidades para garantir que apanha o valor certo
            if raw_status in ['ligado', 'ativo', 'on', 'start', 'funcionamento']:
                status_display = 'ativo'  # Isto ativa a cor VERDE (text-success)
            
            elif raw_status in ['desativado', 'off', 'stop', 'parado']:
                status_display = 'desativado' # Isto ativa a cor VERMELHA (text-danger)
            
            elif 'manuten' in raw_status: # Apanha "manutenção", "manutencao", etc.
                status_display = 'manutencao' # Isto ativa a cor AZUL (text-info)
            
            else:
                # Se não for nenhum dos acima, mantém o valor original para debug visual
                # (ex: se o status for "Avariado", vai aparecer "Avariado" a cinzento)
                status_display = raw_status

            # PASSO C: Verificar MongoDB (Opcional / Complementar)
            # Só tentamos ir ao Mongo se o SQL disser que está Ligado.
            # Se o Mongo falhar, MANTEMOS O 'ativo' do SQL em vez de mudar para 'offline'.
            if status_display == 'ativo' and chiller.ipcontrolador:
                try:
                    mongo_data = get_mongo_data(chiller.ipcontrolador)
                    if mongo_data:
                        mongo_ctx = mongo_data
                        # Se o Mongo disser explicitamente que está parado, atualizamos.
                        # Mas se o Mongo der erro ou vier vazio, mantemos 'ativo'.
                        mongo_status = mongo_data.get('status', '').lower()
                        if mongo_status in ['desativado', 'off', 'alarm']:
                            status_display = mongo_status
                except Exception as e:
                    print(f"Aviso: Não foi possível ler dados Mongo do IP {chiller.ipcontrolador}. Mantendo status SQL.")

        # 6. Atualizar Contexto (Mantém-se igual)
        context.update({
            'cliente': cliente,
            'chiller': chiller,
            'chillers': chillers,
            'logs': logs,
            'horas_atuais': horas_atuais,
            'horas_limite': 10000.0,
            'progresso_percent': progresso_percent,
            
            # AQUI ESTÁ A CHAVE: Enviamos a variável calculada acima
            'chiller_status_realtime': status_display, 
            
            'rendimento': mongo_ctx.get('rendimento'),
            'medidor_values': mongo_ctx.get('medidor')
        })
        
        return context

class RegistarIntervencaoView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        try:
            chiller_id = request.POST.get('chiller_id')
            tecnico = request.POST.get('tecnico')
            tipo = request.POST.get('tipo')
            categoria = request.POST.get('categoria')
            descricao = request.POST.get('descricao')
            
            reset_check = request.POST.get('reset_horimetro') == 'on'
            fugas_check = request.POST.get('teste_fugas') == 'on'
            gas_kg = request.POST.get('gas_kg') or 0.0

            chiller = get_object_or_404(Chiller, idChiller=chiller_id)
            Intervencao.objects.create(
                chiller=chiller,
                tecnico_nome=tecnico,
                tipo=tipo,
                categoria=categoria,
                descricao=descricao,
                gas_adicionado=float(gas_kg),
                teste_fugas_realizado=fugas_check,
                reset_horimetro=reset_check,
                data_intervencao=timezone.now()
            )
            
            if reset_check:
                 chiller.horas_funcionamento = 0.0 
                 chiller.data_ultima_revisao = timezone.now()
                 chiller.save()
                 
            base_url = reverse('livro_obra') 
            return redirect(f"{base_url}?cliente_id={chiller.idCliente.idCliente}&chiller_id={chiller.idChiller}")

        except Exception as e:
            logger.error(f"Erro Intervenção: {e}")
            if 'chiller_id' in request.POST:
                 # Tenta voltar atrás usando REFERER
                 return redirect(request.META.get('HTTP_REFERER', '/dashboard/'))
            return redirect('/dashboard/')