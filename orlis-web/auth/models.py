from django.db import models
from django.contrib.auth.models import User
from apps.dashboard.models import Cliente



class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    email = models.EmailField(max_length=100, unique=True)
    email_token = models.CharField(max_length=100, blank=True, null=True)
    forget_password_token = models.CharField(max_length=100, blank=True, null=True)
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    # ➕ LIGAÇÃO AO CLIENTE
    cliente = models.ForeignKey(
        Cliente,
        on_delete=models.CASCADE,
        related_name="profiles",
        null=True,
        blank=True
    )

    def __str__(self):
        return self.user.username

    class Meta:
        verbose_name = "User Profile"
        verbose_name_plural = "User Profiles"