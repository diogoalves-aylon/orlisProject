from django.shortcuts import redirect
from django.contrib.auth.models import User, Group
from django.contrib import messages
from auth.views import AuthView
from auth.helpers import send_verification_email
import uuid

# Importar modelos
from apps.dashboard.models import Cliente
# IMPORTANTE: Verifica se este caminho corresponde à pasta onde criaste o 'ClienteProfile'
from apps.cliente.models import ClienteProfile 

class RegisterView(AuthView):

    def get(self, request):
        if request.user.is_authenticated:
            return redirect("index")
        return super().get(request)

    def post(self, request):
        try:
            # 1. Recolher dados
            username = request.POST.get("username")
            email = request.POST.get("email")
            password = request.POST.get("password")
            
            empresa_nome = request.POST.get("empresa_nome")
            empresa_nif = request.POST.get("empresa_nif")
            empresa_localidade = request.POST.get("empresa_localidade")
            empresa_telefone = request.POST.get("empresa_telefone")

            # 2. Processar Logo
            logo_file = request.FILES.get("empresa_logo")
            logo_binary = None
            if logo_file:
                logo_binary = logo_file.read()

            # 3. Validações
            if User.objects.filter(username=username).exists():
                messages.error(request, "Username já existe.")
                return redirect("register")

            if User.objects.filter(email=email).exists():
                messages.error(request, "Email já existe.")
                return redirect("register")

            # 4. Criar USER
            user = User.objects.create_user(username=username, email=email)
            user.set_password(password)
            user.save()

            # 5. Criar CLIENTE
            cliente = Cliente.objects.create(
                nome=empresa_nome,
                email=email,
                nif=empresa_nif,
                localidade=empresa_localidade,
                telefone=empresa_telefone,
                logo=logo_binary
            )

            # 6. Criar ASSOCIAÇÃO (ClienteProfile)
            # Aqui usamos o modelo que me enviaste agora
            ClienteProfile.objects.create(
                user=user,
                cliente=cliente
            )

            # 7. Adicionar ao Grupo
            group, _ = Group.objects.get_or_create(name="client")
            user.groups.add(group)

            # 8. Sucesso
            messages.success(request, "Conta criada com sucesso!")
            return redirect("login")

        except Exception as e:
            print(f"ERRO NO REGISTO: {e}")
            # Opcional: Apagar user/cliente se falhar a meio para não deixar lixo
            messages.error(request, "Ocorreu um erro ao criar a conta.")
            return redirect("register")