from django.shortcuts import redirect
from django.contrib.auth import authenticate, login
from django.contrib.auth.models import User
from django.contrib import messages
from auth.views import AuthView

class LoginView(AuthView):
    def get(self, request):
        if request.user.is_authenticated:
            # Se já está logado, manda para o destino correto
            return self.redirect_by_role(request.user)

        return super().get(request)

    def post(self, request):
        username = request.POST.get("email-username")
        password = request.POST.get("password")

        if not (username and password):
            messages.error(request, "Por favor, insira o username e a palavra-passe.")
            return redirect("login")

        if "@" in username:
            user_obj = User.objects.filter(email=username).first()
            if not user_obj:
                messages.error(request, "Email inválido.")
                return redirect("login")
            username = user_obj.username

        authenticated_user = authenticate(request, username=username, password=password)
        if authenticated_user:
            login(request, authenticated_user)

            # Redirecionamento baseado no papel do utilizador
            return self.redirect_by_role(authenticated_user)
        else:
            messages.error(request, "Credenciais inválidas.")
            return redirect("login")

    def redirect_by_role(self, user):
        if user.is_superuser:
            return redirect("/dashboard/") 
        else:
            return redirect("/cliente/")
