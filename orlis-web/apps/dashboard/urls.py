from django.urls import path
from .views import (DashboardView, ChillerDetailView, SensorDataAPIView, ClienteLogoView ,VariableLogView, TableClientsView, ClienteUpdateView,
ClienteDeleteView,RelatorioView, ClienteChillerDetailView, ChillerUpdateView, ChillerDeleteView, LivroObraView, RegistarIntervencaoView)
urlpatterns = [
    path("", DashboardView.as_view(template_name="dashboard.html"), name="dashboard"),
    path("", DashboardView.as_view(template_name="dashboard.html"), name="index"), 
    path("chiller_data/", ChillerDetailView.as_view(template_name="page_2.html"), name="chiller-data"),
    path("page_5/", VariableLogView.as_view(template_name="page_5.html"), name="page-5"),
    path('table_clients/', TableClientsView.as_view(), name='table_clients'),
    path('cliente/update/', ClienteUpdateView.as_view(), name='cliente_update'),
    path("cliente/chillers/", ClienteChillerDetailView.as_view(), name="cliente_chiller_detail"),
    path('cliente/delete/', ClienteDeleteView.as_view(), name='cliente_delete'),
    path("api/sensor-data/", SensorDataAPIView.as_view(), name="sensor-data-api"),
    path('relatorios/', RelatorioView.as_view(), name='relatorios'),
    path('logo/<int:cliente_id>/', ClienteLogoView.as_view(), name='cliente-logo'),
    path('chiller/update/', ChillerUpdateView.as_view(), name='chiller_update'),
    path('chiller/delete/', ChillerDeleteView.as_view(), name='chiller_delete'),
    path('livro-de-obra/', LivroObraView.as_view(), name='livro_obra'),
    path('registar-intervencao/', RegistarIntervencaoView.as_view(), name='registar_intervencao'),   
]
