from django.db import models
from django.utils import timezone

class Cliente(models.Model):
    idCliente = models.AutoField(db_column='idcliente', primary_key=True)
    nome = models.TextField(db_column='nome')
    email = models.TextField(db_column='email')
    logo = models.BinaryField(null=True, blank=True)

    telefone = models.TextField(null=True, blank=True)
    localidade = models.TextField(null=True, blank=True)
    nif = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'cliente'
        managed = True

    def __str__(self):
        return self.nome
    

class Chiller(models.Model):
    idChiller = models.AutoField(db_column='idchiller', primary_key=True)
    nome = models.TextField()
    localizacao = models.TextField()
    latitude = models.DecimalField(
        max_digits=10, decimal_places=8, null=True, blank=True, 
        verbose_name="Latitude"
    )
    longitude = models.DecimalField(
        max_digits=10, decimal_places=8, null=True, blank=True, 
        verbose_name="Longitude"
    )
    ipcontrolador = models.TextField()
    status = models.CharField(max_length=50, choices=[
        ('ligado', 'Ligado'), ('desativado', 'Desativado'), ('manutencao', 'Manutenção')
    ], default='ligado')
    marca = models.TextField(null=True, blank=True)
    modelo = models.TextField(null=True, blank=True)
    n_serie = models.TextField(null=True, blank=True)
    gas = models.TextField(null=True, blank=True)
    ciclo = models.CharField(max_length=20, choices=[
        ('verao', 'Verão'), ('inverno', 'Inverno')
    ], default='verao')

    # --- CMMS / MANUTENÇÃO ---
    horas_funcionamento = models.DecimalField(
        max_digits=10, decimal_places=2, default=0.00, 
        null=True, blank=True, verbose_name="Horas de Funcionamento"
    )
    data_ultima_revisao = models.DateTimeField(
        null=True, blank=True, verbose_name="Data da Última Revisão"
    )

    idCliente = models.ForeignKey(
        Cliente, on_delete=models.CASCADE, db_column='idcliente', related_name='chillers'
    )

    class Meta:
        db_table = 'chiller'
        managed = True 

    def __str__(self):
        return f"{self.nome} ({self.ipcontrolador})"

class Intervencao(models.Model):
    # Tipos Reais
    TIPO_CHOICES = [
        ('Preventiva', 'Preventiva (Plano)'),
        ('Corretiva', 'Corretiva (Avaria)'),
        ('Preditiva', 'Preditiva (Condição)'),
        ('Regulamentar', 'Legal / F-Gas'),
        ('Retrofit', 'Melhoria / Retrofit'),
    ]
    # Categorias Técnicas
    CATEGORIA_CHOICES = [
        ('Compressores', 'Compressores / Óleo'),
        ('Circuito Frigorífico', 'Circuito de Gás'),
        ('Permutadores', 'Limpeza Permutadores'),
        ('Elétrica', 'Elétrica / Comandos'),
        ('Hidráulica', 'Hidráulica'),
        ('Sensores', 'Sensores / Automação'),
    ]

    chiller = models.ForeignKey(Chiller, on_delete=models.CASCADE, related_name='intervencoes')
    tecnico_nome = models.CharField(max_length=100, verbose_name="Técnico")
    tipo = models.CharField(max_length=50, choices=TIPO_CHOICES)
    categoria = models.CharField(max_length=50, choices=CATEGORIA_CHOICES)
    descricao = models.TextField(verbose_name="Relatório Técnico")
    
    # Campos Técnicos Avançados
    gas_adicionado = models.DecimalField(
        max_digits=6, decimal_places=2, default=0.00, 
        verbose_name="Gás Adicionado (kg)"
    )
    teste_fugas_realizado = models.BooleanField(default=False, verbose_name="Teste Fugas")
    reset_horimetro = models.BooleanField(default=False, verbose_name="Reset Horas")

    custo_total = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    componentes_substituidos = models.TextField(null=True, blank=True)
    data_intervencao = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'intervencao'
        ordering = ['-data_intervencao']

    def __str__(self):
        return f"{self.tipo} - {self.data_intervencao.date()}"

class ClienteChillers(Cliente):
    class Meta:
        proxy = True
        verbose_name = 'Cliente com Chillers'
        verbose_name_plural = 'Clientes com Chillers'

    def chillers_associados(self):
        return Chiller.objects.filter(idCliente=self)