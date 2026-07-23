from django.db import models
from django.contrib.auth.models import User
from apps.dashboard.models import Cliente

class ClienteProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    cliente = models.ForeignKey(Cliente, on_delete=models.CASCADE)

    def __str__(self):
        return f"{self.user.username} - {self.cliente.nome}"

#class Cliente(models.Model):
#    idCliente = models.IntegerField(db_column='idcliente', primary_key=True)
#    nome = models.TextField(db_column='nome')
#    email = models.TextField(db_column='email')
#    logo = models.BinaryField(null=True, blank=True)
#
#    class Meta:
#        db_table = 'cliente'
#        #managed = False
#
#    def __str__(self):
#        return self.nome