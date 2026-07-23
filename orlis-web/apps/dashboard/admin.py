from django.utils.html import format_html
from django.contrib import admin
from .models import Cliente
from .forms import ClienteAdminForm 

@admin.register(Cliente)
class ClienteAdmin(admin.ModelAdmin):
    form = ClienteAdminForm
    list_display = ('idCliente', 'nome', 'email', 'logo_preview')
    search_fields = ('nome',)

    readonly_fields = ['logo_preview']

    def logo_preview(self, obj):
        if obj.logo:
            import base64
            logo_b64 = base64.b64encode(obj.logo).decode('utf-8')
            return format_html(f'<img src="data:image/png;base64,{logo_b64}" style="max-height:100px;" />')
        return "Sem logo"

    logo_preview.short_description = "Pré-visualização da Logo"
