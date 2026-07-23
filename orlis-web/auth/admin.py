from django.contrib import admin
from .models import Profile


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "email",
        "cliente",
        "is_verified",
        "created_at",
    )

    search_fields = ("user__username", "email", "cliente__nome")
    list_filter = ("is_verified", "cliente")
    autocomplete_fields = ("user", "cliente")
