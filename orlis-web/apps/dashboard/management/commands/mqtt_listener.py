"""
Listener MQTT de telemetria de chillers.

Substitui o loop de polling Modbus de ibis_prototipo/script/scriptV5.py: os dados
já chegam decodificados via MQTT (contrato acordado, a confirmar quando o lado
Raspberry/PLC for implementado) e são processados por apps.dashboard.services.telemetry.

Correr como instância única (systemd/processo dedicado) — memoria_energia vive em
processo e não é partilhada entre réplicas.
"""
import json
import logging
import os
import ssl
import threading

import paho.mqtt.client as mqtt
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from pymongo import MongoClient

from apps.dashboard.services import telemetry

logger = logging.getLogger(__name__)

DEFAULT_TOPIC = "chillers/+/telemetria"
REQUIRED_PAYLOAD_KEYS = ("ip", "timestamp", "temps", "pressoes", "medidor")


class ChillerMetadataCache:
    """Cache em memória de {ipcontrolador: {nome, gas, ciclo}}, com refresh periódico
    (rede de segurança para alterações feitas no admin) e refresh on-demand para IPs
    desconhecidos (chiller novo)."""

    def __init__(self, refresh_interval_seconds):
        self.refresh_interval_seconds = refresh_interval_seconds
        self._lock = threading.Lock()
        self._data = {}

    def reload(self):
        fresh = telemetry.get_active_chillers()
        with self._lock:
            self._data = fresh
        logger.info("Cache de metadados de chillers atualizado (%d chillers ativos).", len(fresh))
        return fresh

    def get(self, ip):
        with self._lock:
            return self._data.get(ip)

    def snapshot(self):
        with self._lock:
            return dict(self._data)


class Command(BaseCommand):
    help = "Liga a um broker MQTT (opcionalmente com mTLS) e processa a telemetria de chillers publicada no tópico MQTT_TOPIC."
    # Este comando não usa urlconf/request handling — dispensa os system checks do Django
    # (que importariam urls.py/views.py) para não depender de dependências só usadas em PDF/HTTP.
    requires_system_checks = []

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

        refresh_interval = getattr(settings, "MQTT_METADATA_REFRESH_SECONDS", 300)
        cache = ChillerMetadataCache(refresh_interval)

        mongo_client = MongoClient(settings.MONGO_URL)
        collection = mongo_client[settings.MONGO_DB_NAME]["values"]

        chillers = cache.reload()
        if not chillers:
            logger.warning("Nenhum chiller ativo encontrado na BD ao arrancar o listener.")
        telemetry.init_energy_memory(collection, chillers)

        stop_event = threading.Event()
        refresh_thread = threading.Thread(
            target=self._refresh_loop, args=(cache, stop_event), daemon=True, name="mqtt-metadata-refresh"
        )
        refresh_thread.start()

        topic = getattr(settings, "MQTT_TOPIC", None) or DEFAULT_TOPIC

        mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="orlis-mqtt-listener")
        username = getattr(settings, "MQTT_USERNAME", None)
        if username:
            mqtt_client.username_pw_set(username, getattr(settings, "MQTT_PASSWORD", None))

        if getattr(settings, "MQTT_TLS_ENABLED", False):
            self._configure_tls(mqtt_client)

        mqtt_client.user_data_set({"cache": cache, "collection": collection, "topic": topic})
        mqtt_client.on_connect = self._on_connect
        mqtt_client.on_disconnect = self._on_disconnect
        mqtt_client.on_message = self._on_message
        mqtt_client.reconnect_delay_set(min_delay=1, max_delay=120)

        host = getattr(settings, "MQTT_HOST", "localhost")
        port = getattr(settings, "MQTT_PORT", 1883)

        logger.info(
            "A ligar ao broker MQTT %s:%s (TLS=%s, tópico=%s) ...",
            host, port, getattr(settings, "MQTT_TLS_ENABLED", False), topic,
        )
        try:
            mqtt_client.connect(host, port, keepalive=60)
        except Exception as e:
            logger.error("Falha ao ligar ao broker MQTT %s:%s: %s", host, port, e)
            stop_event.set()
            mongo_client.close()
            return

        try:
            mqtt_client.loop_forever(retry_first_connection=True)
        except KeyboardInterrupt:
            logger.info("A parar mqtt_listener (interrupção manual)...")
        finally:
            stop_event.set()
            mqtt_client.disconnect()
            mongo_client.close()

    def _configure_tls(self, mqtt_client):
        """Configura mTLS. Falha no arranque se algum ficheiro faltar: sem isto o paho só se
        queixaria na ligação, e um broker mTLS recusa o handshake sem dizer porquê — o sintoma
        seria um listener a reconectar em silêncio para sempre."""
        ca_certs = getattr(settings, "MQTT_TLS_CA_CERTS", None)
        certfile = getattr(settings, "MQTT_TLS_CERTFILE", None)
        keyfile = getattr(settings, "MQTT_TLS_KEYFILE", None)

        if not ca_certs:
            raise CommandError("MQTT_TLS_ENABLED está ativo mas MQTT_TLS_CA_CERTS não está definido.")
        if bool(certfile) != bool(keyfile):
            raise CommandError(
                "MQTT_TLS_CERTFILE e MQTT_TLS_KEYFILE têm de ser definidos em conjunto "
                "(o broker da Thermia exige mTLS, portanto ambos)."
            )

        for label, path in (("MQTT_TLS_CA_CERTS", ca_certs), ("MQTT_TLS_CERTFILE", certfile), ("MQTT_TLS_KEYFILE", keyfile)):
            if path and not os.path.isfile(path):
                raise CommandError(f"{label} aponta para um ficheiro que não existe: {path}")

        mqtt_client.tls_set(
            ca_certs=ca_certs,
            certfile=certfile,
            keyfile=keyfile,
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )

        if getattr(settings, "MQTT_TLS_INSECURE", False):
            # Necessário quando se liga por um nome que o certificado do broker não cobre
            # (túnel SSH para localhost). Cifra mantém-se; cai a garantia de identidade.
            mqtt_client.tls_insecure_set(True)
            logger.warning("MQTT_TLS_INSECURE ativo: a verificação do hostname do broker está desligada.")

        logger.info("mTLS configurado (CA=%s, cert=%s).", ca_certs, certfile or "nenhum")

    def _refresh_loop(self, cache, stop_event):
        while not stop_event.wait(cache.refresh_interval_seconds):
            try:
                cache.reload()
            except Exception as e:
                logger.error("Erro ao atualizar cache de metadados de chillers: %s", e)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            topic = userdata["topic"]
            logger.info("Ligado ao broker MQTT. A subscrever %s", topic)
            client.subscribe(topic, qos=1)
        else:
            logger.error("Falha na ligação ao broker MQTT (reason_code=%s)", reason_code)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        if reason_code != 0:
            logger.warning("Ligação MQTT perdida (reason_code=%s). A tentar reconectar automaticamente...", reason_code)
        else:
            logger.info("Desligado do broker MQTT.")

    def _on_message(self, client, userdata, msg):
        received_at = telemetry.agora_local()  # hora de Lisboa, imune ao TIME_ZONE="UTC" do Django
        cache = userdata["cache"]
        collection = userdata["collection"]

        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            logger.error("Payload inválido em %s: %s", msg.topic, e)
            return

        missing = [k for k in REQUIRED_PAYLOAD_KEYS if k not in payload]
        if missing:
            logger.error("Payload em %s sem campos obrigatórios %s: %s", msg.topic, missing, payload)
            return

        ip = payload["ip"]
        logger.info("Mensagem recebida de %s (tópico %s)", ip, msg.topic)

        meta = cache.get(ip)
        if meta is None:
            logger.warning("IP %s desconhecido no cache — a forçar refresh imediato.", ip)
            meta = cache.reload().get(ip)
        if meta is None:
            logger.error("IP %s não corresponde a nenhum chiller ativo na BD. Mensagem descartada.", ip)
            return

        raw_data = {
            "temps": payload.get("temps", {}),
            "pressoes": payload.get("pressoes", {}),
            "medidor": payload.get("medidor", {}),
        }

        try:
            telemetry.process_chiller(
                ip=ip,
                nome=meta["nome"],
                timestamp=payload["timestamp"],
                raw_data=raw_data,
                collection=collection,
                fluido_db=meta["gas"],
                ciclo_db=meta["ciclo"],
                received_at=received_at,
            )
        except Exception as e:
            logger.error("Erro ao processar telemetria de %s: %s", ip, e, exc_info=True)
