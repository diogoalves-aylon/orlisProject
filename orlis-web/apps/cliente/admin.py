from django.contrib import admin
from .models import ClienteProfile

@admin.register(ClienteProfile)
class ClienteProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'cliente')
