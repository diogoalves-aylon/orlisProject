"""
Listener MQTT de telemetria de chillers.

Substitui o loop de polling Modbus de ibis_prototipo/script/scriptV5.py: os dados já
chegam decodificados via MQTT e são processados por apps.dashboard.services.telemetry.

O Raspberry publica os blocos de leituras dentro de "values" (a forma do documento final
do Mongo); o contrato inicial punha-os no topo do payload. As duas formas são aceites —
ver _normalizar_payload().

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

# Só o tópico do Raspberry. MQTT_TOPIC aceita vários filtros separados por vírgula — o do
# simulador (chillers/<ip>/telemetria) acrescenta-se no .env local, nunca no do servidor:
# ver a nota em config/settings.py.
DEFAULT_TOPIC = "iot/raspberry-aylon/#"

# Campos que têm de vir no topo do payload...
REQUIRED_PAYLOAD_KEYS = ("ip", "timestamp")
# ... e blocos de leituras, que podem vir no topo ou dentro de "values" (ver _normalizar_payload).
BLOCOS_TELEMETRIA = ("temps", "pressoes", "medidor")


def _topicos(valor):
    """Divide a string de MQTT_TOPIC numa lista de filtros de subscrição."""
    return [t.strip() for t in (valor or "").split(",") if t.strip()]


def _normalizar_payload(payload):
    """Aplana o payload MQTT para a forma que process_chiller() espera.

    O contrato inicial punha temps/pressoes/medidor no topo do payload, mas o Raspberry
    publica-os dentro de "values", com a forma do documento final do Mongo:

        {"timestamp": ..., "ip": ..., "chiller": ..., "fluido": ..., "ciclo": ...,
         "values": {"temps": {...}, "pressoes": {...}, "medidor": {...}}}

    Aceitar as duas formas é o que faz a telemetria real chegar ao MongoDB: a validação de
    campos obrigatórios não encontrava temps/pressoes/medidor no topo e descartava TODAS as
    mensagens do broker de produção, mesmo com a ligação mTLS e a subscrição a funcionar.

    Devolve (dados_normalizados, erro) — só um dos dois é preenchido.
    """
    if not isinstance(payload, dict):
        return None, f"o payload não é um objeto JSON (é {type(payload).__name__})"

    values = payload.get("values")
    if values is None:
        blocos = payload
    elif isinstance(values, dict):
        blocos = values
    else:
        return None, f"o campo 'values' existe mas não é um objeto (é {type(values).__name__})"

    faltam = [k for k in REQUIRED_PAYLOAD_KEYS if not payload.get(k)]
    faltam += [k for k in BLOCOS_TELEMETRIA if not isinstance(blocos.get(k), dict)]
    if faltam:
        return None, f"campos obrigatórios em falta ou inválidos: {faltam}"

    return {
        "ip": payload["ip"],
        "timestamp": payload["timestamp"],
        "temps": blocos["temps"],
        "pressoes": blocos["pressoes"],
        "medidor": blocos["medidor"],
        # Metadados que o payload já traz. A BD manda, quando o IP está registado; estes
        # servem de recurso para não perder a leitura (ver _meta_do_payload).
        "chiller": payload.get("chiller"),
        "fluido": payload.get("fluido"),
        "ciclo": payload.get("ciclo"),
    }, None


class ChillerMetadataCache:
    """Cache em memória de {ipcontrolador: {nome, gas, ciclo}}, com refresh periódico
    (rede de segurança para alterações feitas no admin) e refresh on-demand para IPs
    desconhecidos (chiller novo)."""

    def __init__(self, refresh_interval_seconds):
        self.refresh_interval_seconds = refresh_interval_seconds
        self._lock = threading.Lock()
        self._data = {}
        # IPs que já falharam o refresh on-demand. Sem esta marca, um chiller que publica
        # mas não está registado na BD provoca uma query ao Postgres por cada mensagem.
        self._desconhecidos = set()

    def reload(self):
        fresh = telemetry.get_active_chillers()
        with self._lock:
            self._data = fresh
            # Um IP que passou a estar registado deixa de ser desconhecido; os outros
            # mantêm a marca, para não voltarem a forçar refreshes.
            self._desconhecidos -= fresh.keys()
        logger.info("Cache de metadados de chillers atualizado (%d chillers ativos).", len(fresh))
        return fresh

    def get(self, ip):
        with self._lock:
            return self._data.get(ip)

    def resolve(self, ip):
        """Metadados do IP, forçando um refresh se ele não estiver no cache — mas só na
        primeira mensagem desse IP.

        Devolve (meta, primeira_falha): meta é None se o IP não estiver registado, e
        primeira_falha é True só nessa primeira vez, para o chamador avisar uma vez em
        vez de a cada mensagem.
        """
        meta = self.get(ip)
        if meta is not None:
            return meta, False

        with self._lock:
            if ip in self._desconhecidos:
                return None, False

        logger.warning("IP %s desconhecido no cache — a forçar refresh imediato.", ip)
        meta = self.reload().get(ip)
        if meta is not None:
            return meta, False

        with self._lock:
            self._desconhecidos.add(ip)
        return None, True

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

        topics = _topicos(getattr(settings, "MQTT_TOPIC", None)) or _topicos(DEFAULT_TOPIC)

        mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="orlis-mqtt-listener")
        username = getattr(settings, "MQTT_USERNAME", None)
        if username:
            mqtt_client.username_pw_set(username, getattr(settings, "MQTT_PASSWORD", None))

        if getattr(settings, "MQTT_TLS_ENABLED", False):
            self._configure_tls(mqtt_client)

        mqtt_client.user_data_set({"cache": cache, "collection": collection, "topics": topics})
        mqtt_client.on_connect = self._on_connect
        mqtt_client.on_disconnect = self._on_disconnect
        mqtt_client.on_message = self._on_message
        mqtt_client.reconnect_delay_set(min_delay=1, max_delay=120)

        host = getattr(settings, "MQTT_HOST", "localhost")
        port = getattr(settings, "MQTT_PORT", 1883)

        logger.info(
            "A ligar ao broker MQTT %s:%s (TLS=%s, tópicos=%s) ...",
            host, port, getattr(settings, "MQTT_TLS_ENABLED", False), ", ".join(topics),
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
            topics = userdata["topics"]
            logger.info("Ligado ao broker MQTT. A subscrever %s", ", ".join(topics))
            client.subscribe([(t, 1) for t in topics])
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

        dados, erro = _normalizar_payload(payload)
        if erro:
            logger.error("Payload inválido em %s (%s): %s", msg.topic, erro, payload)
            return

        ip = dados["ip"]
        logger.info("Mensagem recebida de %s (tópico %s)", ip, msg.topic)

        meta, primeira_falha = cache.resolve(ip)
        if meta is None:
            meta = self._meta_do_payload(dados, ip, avisar=primeira_falha)

        raw_data = {
            "temps": dados["temps"],
            "pressoes": dados["pressoes"],
            "medidor": dados["medidor"],
        }

        try:
            telemetry.process_chiller(
                ip=ip,
                nome=meta["nome"],
                timestamp=dados["timestamp"],
                raw_data=raw_data,
                collection=collection,
                fluido_db=meta["gas"],
                ciclo_db=meta["ciclo"],
                received_at=received_at,
            )
        except Exception as e:
            logger.error("Erro ao processar telemetria de %s: %s", ip, e, exc_info=True)

    @staticmethod
    def _meta_do_payload(dados, ip, avisar):
        """Metadados de recurso para um IP que não está registado como chiller ativo na BD.

        Antes a mensagem era descartada aqui. Registar o chiller no admin é trabalho de
        configuração, e perder telemetria por causa disso é pior do que gravá-la: o payload
        já traz nome, fluido e ciclo, portanto grava-se com o que ele diz. O que se perde é
        o horímetro (increment_hours() não encontra a linha e avisa) e a garantia de que o
        nome coincide com o da BD.
        """
        meta = {
            "nome": dados.get("chiller") or ip,
            "gas": dados.get("fluido") or telemetry.DEFAULT_FLUIDO,
            "ciclo": dados.get("ciclo") or "verao",
        }
        if avisar:
            logger.warning(
                "IP %s não corresponde a nenhum chiller ativo na BD — a telemetria vai ser "
                "gravada no MongoDB com os metadados do próprio payload (nome=%r, fluido=%s, "
                "ciclo=%s). Para o dashboard a mostrar e o horímetro contar, registar o chiller "
                "com ipcontrolador=%s.",
                ip, meta["nome"], meta["gas"], meta["ciclo"], ip,
            )
        return meta
