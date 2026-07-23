from rest_framework import serializers

class SensorDataSerializer(serializers.Serializer):
    chiller = serializers.CharField()
    fluido = serializers.CharField()
    ip = serializers.CharField()
    timestamp = serializers.DateTimeField()
    
    # Temperaturas
    T1_output = serializers.FloatField()
    T2_output = serializers.FloatField()
    T3_output = serializers.FloatField()
    T4_output = serializers.FloatField()
    T5_output = serializers.FloatField(required=False, allow_null=True)
    
    # Pressões
    P1_output = serializers.FloatField()
    P2_output = serializers.FloatField()
    
    # Entalpias
    h1 = serializers.FloatField()
    estado_h1 = serializers.CharField()
    h2 = serializers.FloatField()
    estado_h2 = serializers.CharField()
    h3 = serializers.FloatField()
    estado_h3 = serializers.CharField()
    h4 = serializers.FloatField()
    estado_h4 = serializers.CharField()
    
    # Medidor Elétrico
    Corrente_L1_output = serializers.FloatField()
    Corrente_L2_output = serializers.FloatField()
    Corrente_L3_output = serializers.FloatField()
    MediaCorrentes_output = serializers.FloatField()
    PotenciaAbsorbida_kW = serializers.FloatField()
    EnergiaAtivaParcial_output = serializers.FloatField()
    EnergiaReativaParcial_output = serializers.FloatField()
    EnergiaAtivaTotal_output = serializers.FloatField()
    EnergiaReativaTotal_output = serializers.FloatField()
    Tensao_L1_L2_output = serializers.FloatField()
    Tensao_L2_L3_output = serializers.FloatField()
    Tensao_L3_L1_output = serializers.FloatField()
    MediaTensoes_output = serializers.FloatField()
    Consumo_hora = serializers.FloatField()
    Custo_hora = serializers.FloatField()
    
    # Métricas
    COP = serializers.FloatField()
