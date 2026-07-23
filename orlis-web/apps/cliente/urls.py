from django.urls import path
from .views import (
    ClienteChillersView,
    ClienteChillerDetailView,
    VariableLogView,
    RelatorioView,
    ClienteLogoView,
    PerfilView,
    ClienteLivroObraView,
)

urlpatterns = [
    # --- Dashboard Principal ---
    path("", ClienteChillersView.as_view(), name="cliente-chillers"),

    # --- Detalhes e Dados em Tempo Real ---
    path("chiller_data/", ClienteChillerDetailView.as_view(), name="cliente-page-2"),

    # --- Logs e Gráficos ---
    path("logs/", VariableLogView.as_view(), name="cliente-logs"),

    # --- Relatórios ---
    path("relatorio/", RelatorioView.as_view(), name="cliente-relatorio"),

    # --- Manutenção / Livro de Obra ---
    path("livro-obra/", ClienteLivroObraView.as_view(), name="cliente-livro-obra"),

    # --- Utilitários (Perfil e Logo) ---
    path('logo/<int:cliente_id>/', ClienteLogoView.as_view(), name='cliente-logo'),
    path("perfil/", PerfilView.as_view(), name="pages-profile-user"),
]