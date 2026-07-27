#!/usr/bin/env python3
"""
Simulador do Raspberry/PLC para o listener MQTT do orlis-web.

Publica em chillers/<ip>/telemetria com o payload do contrato, na forma exata dos
documentos reais já gravados no MongoDB (temps T1-T5, medidor completo).

O timestamp é gerado em hora local de Lisboa (naive), que é a referência usada por
apps.dashboard.services.telemetry.agora_local() — assim não dispara o aviso de skew.

Perfis de pressão (--perfil):
  coerente  : ciclo fisicamente válido (P1 baixa ~4 bar) -> COP calcula
  real2407  : replica os valores dos docs de 24/07 (P1 17.3 bar) -> COP fica null

Uso (a partir da raiz do projeto Django, com o listener a correr noutro terminal):
    # uma mensagem, ciclo coerente
    python tools/simular_chiller_mqtt.py

    # loop a cada 30s, contador de energia a subir (para ver custo/hora)
    python tools/simular_chiller_mqtt.py --loop 30

    # reproduzir o problema do COP null dos dados de 24/07
    python tools/simular_chiller_mqtt.py --perfil real2407

    # simular standby (corrente abaixo do limiar de 2.0 A)
    python tools/simular_chiller_mqtt.py --standby

    # testar a validação de timestamp (fallback para hora de receção)
    python tools/simular_chiller_mqtt.py --skew-horas 3
"""
import argparse
import json
import random
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import paho.mqtt.publish as publish

FUSO = ZoneInfo("Europe/Lisbon")

# Ciclo de arrefecimento R407C fisicamente coerente (pressões RELATIVAS em bar —
# calc_entalpia() soma 1.01325 para obter a absoluta).
#   P1 = baixa/aspiração  -> saturação: bolha -3.8 C, orvalho  2.4 C
#   P2 = alta/descarga    -> saturação: bolha 46.7 C, orvalho 51.3 C
#   T1 = aspiração do compressor (vapor superaquecido: 12 C > orvalho 2.4 C)
#   T2 = descarga do compressor  (vapor superaquecido: 78 C > orvalho 51.3 C)
#   T3 = saída do condensador    (líquido sub-resfriado: 40 C < bolha 46.7 C)
#   T4 = saída do evaporador     (vapor superaquecido:    8 C > orvalho 2.4 C)
#   T5 = sensor extra (água), não usado nos cálculos do ciclo
PERFIL_COERENTE = {
    "temps": {"T1": 12.0, "T2": 78.0, "T3": 40.0, "T4": 8.0, "T5": 10.7},
    "pressoes": {"P1": 4.0, "P2": 19.5},
}

# Valores tal como vieram nos documentos reais de 2026-07-24 18:55:50.
# T4=43.1 C @ P1=17.3 bar cai na zona bifásica -> PropsSI falha -> h4 None -> COP null.
PERFIL_REAL_2407 = {
    "temps": {"T1": 18.4, "T2": 73.5, "T3": 42.8, "T4": 43.1, "T5": 10.7},
    "pressoes": {"P1": 17.30580951198996, "P2": 19.512127856148044},
}

PERFIS = {"coerente": PERFIL_COERENTE, "real2407": PERFIL_REAL_2407}


def agora_lisboa():
    return datetime.now(FUSO).replace(tzinfo=None)


def build_payload(ip, kwh, kvarh, standby, skew_horas, perfil, jitter):
    ts = agora_lisboa() + timedelta(hours=skew_horas)
    base = PERFIS[perfil]

    def j(v):
        return round(v + random.uniform(-jitter, jitter), 2) if jitter else v

    temps = {k: j(v) for k, v in base["temps"].items()}
    pressoes = dict(base["pressoes"])

    if standby:
        correntes = [0.4, 0.35, 0.42]
        potencia = 0.2
    else:
        correntes = [j(25.96), j(26.24), j(24.93)]
        potencia = 13.4

    medidor = {
        "Corrente_L1_output": correntes[0],
        "Corrente_L2_output": correntes[1],
        "Corrente_L3_output": correntes[2],
        "Tensao_L1_L2_output": j(414.73),
        "Tensao_L2_L3_output": j(415.12),
        "Tensao_L3_L1_output": j(415.04),
        "EnergiaAtivaParcial_output": round(kwh, 3),
        "EnergiaReativaParcial_output": round(kvarh, 3),
        "EnergiaAtivaTotal_output": round(kwh, 3),
        "EnergiaReativaTotal_output": round(kvarh, 3),
        "PotenciaAtivaTotal_kW": potencia,
    }

    return {
        "ip": ip,
        "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "temps": temps,
        "pressoes": pressoes,
        "medidor": medidor,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--ip", default="10.25.4.2", help="ipcontrolador do chiller na BD")
    p.add_argument("--perfil", choices=sorted(PERFIS), default="coerente")
    p.add_argument("--kwh", type=float, default=14839.662, help="leitura inicial do contador ativo")
    p.add_argument("--kwh-passo", type=float, default=0.67, help="incremento de kWh por mensagem em --loop")
    p.add_argument("--loop", type=int, metavar="SEGUNDOS", help="publicar repetidamente a cada N segundos")
    p.add_argument("--n", type=int, help="parar depois de N mensagens (só com --loop)")
    p.add_argument("--standby", action="store_true", help="corrente abaixo do limiar (0.4 A)")
    p.add_argument("--skew-horas", type=float, default=0.0, help="desvio a somar ao timestamp")
    p.add_argument("--jitter", type=float, default=0.3, help="ruído aleatório nas leituras (0 = valores fixos)")
    args = p.parse_args()

    kwh = args.kwh
    kvarh = args.kwh + 358.9  # mantém a relação vista nos dados reais
    topic = f"chillers/{args.ip}/telemetria"
    enviadas = 0

    while True:
        payload = build_payload(args.ip, kwh, kvarh, args.standby, args.skew_horas, args.perfil, args.jitter)
        publish.single(topic, json.dumps(payload), hostname=args.host, port=args.port, qos=1)
        enviadas += 1
        m = payload["medidor"]
        print(f"-> {topic}  ts={payload['timestamp']}  perfil={args.perfil}  "
              f"kWh={m['EnergiaAtivaTotal_output']}  I={m['Corrente_L1_output']}A")

        if not args.loop or (args.n and enviadas >= args.n):
            break
        kwh += args.kwh_passo
        kvarh += args.kwh_passo
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
