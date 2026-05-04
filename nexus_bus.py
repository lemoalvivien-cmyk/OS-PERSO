"""HERMES OMEGA - Nexus Bus (Message Bus Inter-Modules)

Bus de communication inter-modules via Redis Streams avec fallback
automatique sur fichiers JSON si Redis est indisponible.

Ports API: 9305
Channels: "brain", "watcher", "scraper", "genesis", "evolution", "knowledge", "system"
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Annotated

import redis.asyncio as aioredis

logger = logging.getLogger("hermes.nexus.bus")

# ─── Constantes ───────────────────────────────────────────────────────────────

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
API_PORT = int(os.getenv("NEXUS_API_PORT", "9305"))

# Canaux valides du système HERMES OMEGA
VALID_CHANNELS = {
    "brain", "watcher", "scraper", "genesis",
    "evolution", "knowledge", "system",
}

# Répertoire fallback pour le stockage fichier
FALLBACK_BASE_DIR = Path.home() / ".hermes-omega" / "bus"

# Durée de vie du heartbeat avant qu'un module soit considéré mort (secondes)
HEARTBEAT_TTL = 90

# Intervalle par défaut du heartbeat (secondes)
HEARTBEAT_INTERVAL = 30

# Limite de retry pour les messages en échec
MAX_RETRIES = 5

# Backoff exponentiel initial (secondes)
RETRY_BASE_DELAY = 1.0

# Dossier dead letter queue
DLQ_DIR = "dead_letter_queue"


# ─── Format de message ────────────────────────────────────────────────────────

@dataclass
class NexusMessage:
    """Message standardisé circulant sur le bus."""
    id: str = ""
    channel: str = ""
    data: dict = field(default_factory=dict)
    timestamp: str = ""
    sender: str = ""
    retry_count: int = 0
    reply_to: Optional[str] = None
    correlation_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Sérialise le message en dictionnaire."""
        return {
            "id": self.id,
            "channel": self.channel,
            "data": self.data,
            "timestamp": self.timestamp,
            "sender": self.sender,
            "retry_count": self.retry_count,
            "reply_to": self.reply_to,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> NexusMessage:
        """Désérialise un dictionnaire en message."""
        return cls(
            id=raw.get("id", ""),
            channel=raw.get("channel", ""),
            data=raw.get("data", {}),
            timestamp=raw.get("timestamp", ""),
            sender=raw.get("sender", ""),
            retry_count=raw.get("retry_count", 0),
            reply_to=raw.get("reply_to"),
            correlation_id=raw.get("correlation_id"),
        )

    @staticmethod
    def now_iso() -> str:
        """Retourne le timestamp UTC actuel en ISO 8601."""
        return datetime.now(timezone.utc).isoformat()


# ─── Backend abstrait ────────────────────────────────────────────────────────

class BusBackend:
    """Interface abstraite pour le backend de stockage des messages."""

    async def publish(self, channel: str, message: dict) -> str:
        raise NotImplementedError

    async def create_group(self, channel: str, group: str) -> bool:
        raise NotImplementedError

    async def read_group(
        self, channel: str, group: str, consumer: str,
        count: int = 10, block_ms: int = 5000,
    ) -> list[dict]:
        raise NotImplementedError

    async def ack(self, channel: str, group: str, message_id: str) -> bool:
        raise NotImplementedError

    async def pending_count(self, channel: str, group: str) -> int:
        raise NotImplementedError


# ─── Backend Redis Streams ───────────────────────────────────────────────────

class RedisBackend(BusBackend):
    """Backend Redis Streams — préféré pour la performance et la fiabilité."""

    def __init__(self, url: str = REDIS_URL):
        self.url = url
        self._redis: Optional[aioredis.Redis] = None

    async def connect(self) -> None:
        """Établit la connexion Redis."""
        try:
            self._redis = aioredis.from_url(
                self.url, decode_responses=True,
                max_connections=20, retry_on_timeout=True,
            )
            await self._redis.ping()
            logger.info("Connexion Redis établie: %s", self.url)
        except Exception as exc:
            logger.warning("Impossible de se connecter à Redis: %s", exc)
            self._redis = None
            raise

    async def close(self) -> None:
        """Ferme la connexion Redis."""
        if self._redis:
            await self._redis.close()
            self._redis = None

    @property
    def available(self) -> bool:
        return self._redis is not None

    def _stream_key(self, channel: str) -> str:
        return f"hermes:bus:{channel}"

    async def publish(self, channel: str, message: dict) -> str:
        key = self._stream_key(channel)
        # Aplatir les dicts/lists en JSON strings pour Redis
        flat = {}
        for k, v in message.items():
            if isinstance(v, (dict, list)):
                flat[k] = json.dumps(v, ensure_ascii=False)
            elif v is not None:
                flat[k] = v
        result = await self._redis.xadd(key, flat, maxlen=100_000)
        return str(result)

    async def create_group(self, channel: str, group: str) -> bool:
        key = self._stream_key(channel)
        try:
            await self._redis.xgroup_create(key, group, id="0", mkstream=True)
            logger.debug("Consumer group '%s' créé sur le channel '%s'", group, channel)
            return True
        except Exception as exc:
            if "BUSYGROUP" in str(exc):
                return True  # Le groupe existe déjà
            logger.warning("Erreur création groupe %s/%s: %s", channel, group, exc)
            return False

    async def read_group(
        self, channel: str, group: str, consumer: str,
        count: int = 10, block_ms: int = 5000,
    ) -> list[dict]:
        key = self._stream_key(channel)
        results = await self._redis.xreadgroup(
            group, consumer, {key: ">"}, count=count, block=block_ms,
        )
        messages: list[dict] = []
        if results:
            for _stream_key, stream_messages in results:
                for msg_id, fields in stream_messages:
                    messages.append({
                        "id": msg_id,
                        "channel": channel,
                        "data": fields,
                    })
        return messages

    async def ack(self, channel: str, group: str, message_id: str) -> bool:
        key = self._stream_key(channel)
        try:
            await self._redis.xack(key, group, message_id)
            return True
        except Exception as exc:
            logger.warning("Erreur ACK %s sur %s/%s: %s", message_id, channel, group, exc)
            return False

    async def pending_count(self, channel: str, group: str) -> int:
        key = self._stream_key(channel)
        try:
            info = await self._redis.xpending_range(key, group, min="-", max="+", count=1)
            return len(info) if info else 0
        except Exception:
            return 0


# ─── Backend fichiers JSON (fallback) ────────────────────────────────────────

class FileBackend(BusBackend):
    """Backend fichiers JSON — utilisé quand Redis est indisponible."""

    def __init__(self, base_dir: Path = FALLBACK_BASE_DIR):
        self.base_dir = base_dir
        self._locks: dict[str, asyncio.Lock] = {}

    def _channel_dir(self, channel: str) -> Path:
        d = self.base_dir / channel / "messages"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _dlq_dir(self) -> Path:
        d = self.base_dir / DLQ_DIR
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _get_lock(self, channel: str) -> asyncio.Lock:
        if channel not in self._locks:
            self._locks[channel] = asyncio.Lock()
        return self._locks[channel]

    async def publish(self, channel: str, message: dict) -> str:
        msg_id = message.get("id", uuid.uuid4().hex)
        message["id"] = msg_id
        d = self._channel_dir(channel)
        lock = self._get_lock(channel)
        async with lock:
            filepath = d / f"{msg_id}.json"
            filepath.write_text(json.dumps(message, ensure_ascii=False, indent=2))
        return msg_id

    async def create_group(self, channel: str, group: str) -> bool:
        # Pour le backend fichier, on crée un fichier de métadonnées pour le groupe
        meta_dir = self.base_dir / channel / "groups"
        meta_dir.mkdir(parents=True, exist_ok=True)
        meta_file = meta_dir / f"{group}.json"
        if not meta_file.exists():
            meta_file.write_text(json.dumps({
                "created_at": NexusMessage.now_iso(),
                "last_ack": None,
            }))
        return True

    async def read_group(
        self, channel: str, group: str, consumer: str,
        count: int = 10, block_ms: int = 5000,
    ) -> list[dict]:
        d = self._channel_dir(channel)
        lock = self._get_lock(channel)
        async with lock:
            # État du consumer — fichier dans groups/
            state_dir = self.base_dir / channel / "groups" / group / "consumers"
            state_dir.mkdir(parents=True, exist_ok=True)
            state_file = state_dir / f"{consumer}.json"

            if state_file.exists():
                state = json.loads(state_file.read_text())
                last_id = state.get("last_id", "")
            else:
                last_id = ""

            all_files = sorted(d.glob("*.json"), key=lambda f: f.stat().st_mtime)
            messages: list[dict] = []
            new_last_id = last_id

            for fp in all_files:
                fid = fp.stem
                if fid <= last_id:
                    continue
                if len(messages) >= count:
                    break
                try:
                    raw = json.loads(fp.read_text())
                    raw["id"] = fid
                    messages.append(raw)
                    new_last_id = fid
                except Exception as exc:
                    logger.warning("Erreur lecture fichier %s: %s", fp, exc)

            # Met à jour la position du consumer
            state_file.write_text(json.dumps({
                "last_id": new_last_id,
                "updated_at": NexusMessage.now_iso(),
            }))

        # Si aucun message, attendre un peu (simuler le blocage)
        if not messages:
            await asyncio.sleep(block_ms / 1000)

        return messages

    async def ack(self, channel: str, group: str, message_id: str) -> bool:
        # En mode fichier, l'ACK supprime le fichier de message
        filepath = self._channel_dir(channel) / f"{message_id}.json"
        if filepath.exists():
            filepath.unlink()
            return True
        return False

    async def pending_count(self, channel: str, group: str) -> int:
        d = self._channel_dir(channel)
        return len(list(d.glob("*.json")))


# ─── Nexus Bus principal ────────────────────────────────────────────────────

class NexusBus:
    """Communication hub entre tous les modules HERMES OMEGA.

    Gère la publication, la souscription, le pattern request/response,
    le broadcast, les heartbeats et la dead letter queue.
    """

    def __init__(self, sender_name: str = "unknown"):
        self.sender_name = sender_name
        self._redis_backend = RedisBackend()
        self._file_backend = FileBackend()
        self._backend: Optional[BusBackend] = None
        self._using_redis = False

        # Registry des consumers actifs
        self._consumers: dict[str, set[str]] = {}  # channel -> {consumer_names}
        # Registry des heartbeats
        self._heartbeats: dict[str, float] = {}  # module -> dernier timestamp

        # Queue pour les réponses request/response
        self._pending_requests: dict[str, asyncio.Future] = {}

        # Tâches d'arrière-plan
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._dlq_task: Optional[asyncio.Task] = None

    # ── Connexion ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialise le bus et sélectionne le backend."""
        try:
            await self._redis_backend.connect()
            self._backend = self._redis_backend
            self._using_redis = True
            logger.info("Nexus Bus démarré avec Redis Streams")
        except Exception:
            self._backend = self._file_backend
            self._using_redis = False
            logger.warning("Nexus Bus démarré en mode fallback (fichiers JSON)")

        # Démarrage des tâches d'arrière-plan
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._dlq_task = asyncio.create_task(self._dlq_reprocess_loop())

    async def stop(self) -> None:
        """Arrête proprement le bus."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._dlq_task:
            self._dlq_task.cancel()
        if self._using_redis:
            await self._redis_backend.close()
        logger.info("Nexus Bus arrêté")

    @property
    def backend_type(self) -> str:
        """Retourne le type de backend actif."""
        return "redis" if self._using_redis else "file"

    # ── Publication ───────────────────────────────────────────────────────

    async def publish(
        self,
        channel: str,
        message: dict,
        sender: Optional[str] = None,
    ) -> str:
        """Publie un message sur un channel et retourne son ID.

        Args:
            channel: Canal de destination (ex: "brain", "watcher").
            message: Données du message (dict arbitraire).
            sender: Nom de l'émetteur (par défaut self.sender_name).

        Returns:
            L'identifiant unique du message.
        """
        if channel not in VALID_CHANNELS:
            raise ValueError(f"Canal invalide: '{channel}'. "
                             f"Canaux valides: {VALID_CHANNELS}")

        msg = NexusMessage(
            id=uuid.uuid4().hex[:16],
            channel=channel,
            data=message,
            timestamp=NexusMessage.now_iso(),
            sender=sender or self.sender_name,
        )

        msg_id = await self._backend.publish(channel, msg.to_dict())
        msg.id = msg_id
        logger.debug("Message publié sur '%s': %s", channel, msg_id)
        return msg_id

    # ── Souscription ─────────────────────────────────────────────────────

    async def subscribe(
        self,
        channel: str,
        group: str,
        consumer: str,
    ) -> AsyncIterator[NexusMessage]:
        """Consomme les messages d'un channel via un consumer group.

        Args:
            channel: Canal à écouter.
            group: Nom du consumer group.
            consumer: Nom unique du consumer dans le groupe.

        Yields:
            NexusMessage: Messages arrivant sur le canal.
        """
        if channel not in VALID_CHANNELS:
            raise ValueError(f"Canal invalide: '{channel}'")

        # Crée le consumer group si nécessaire
        await self._backend.create_group(channel, group)

        # Enregistre le consumer
        if channel not in self._consumers:
            self._consumers[channel] = set()
        self._consumers[channel].add(f"{group}/{consumer}")
        logger.info("Consumer '%s' inscrit sur '%s' (groupe: %s)",
                     consumer, channel, group)

        while True:
            try:
                raw_messages = await self._backend.read_group(
                    channel, group, consumer, count=10, block_ms=5000,
                )
                for raw in raw_messages:
                    msg = NexusMessage.from_dict(raw)
                    try:
                        yield msg
                        # ACK le message après traitement réussi
                        await self._backend.ack(channel, group, msg.id)
                    except Exception as exc:
                        logger.error(
                            "Erreur traitement message %s: %s (retry %d/%d)",
                            msg.id, exc, msg.retry_count, MAX_RETRIES,
                        )
                        msg.retry_count += 1
                        if msg.retry_count >= MAX_RETRIES:
                            await self._send_to_dlq(msg, exc)
                        else:
                            # Republie avec retry_count incrémenté
                            await self._retry_message(msg)
            except asyncio.CancelledError:
                logger.info("Consumer '%s' arrêté sur '%s'", consumer, channel)
                break
            except Exception as exc:
                logger.error(
                    "Erreur lecture canal '%s': %s — retry dans 5s",
                    channel, exc,
                )
                await asyncio.sleep(5)

    # ── Request / Response ───────────────────────────────────────────────

    async def request(
        self,
        channel: str,
        message: dict,
        timeout: float = 30.0,
    ) -> dict:
        """Pattern request-response via un canal temporaire.

        Publie un message sur le canal cible et attend une réponse
        sur un canal dédié à la corrélation.

        Args:
            channel: Canal cible.
            message: Données de la requête.
            timeout: Délai maximum d'attente en secondes.

        Returns:
            La réponse du module cible.

        Raises:
            asyncio.TimeoutError: Si aucune réponse dans le délai imparti.
        """
        correlation_id = uuid.uuid4().hex
        reply_channel = f"{channel}:reply"

        # Enregistre un future pour la réponse
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_requests[correlation_id] = future

        # Publie la requête avec le correlation_id
        msg = NexusMessage(
            id=uuid.uuid4().hex[:16],
            channel=channel,
            data=message,
            timestamp=NexusMessage.now_iso(),
            sender=self.sender_name,
            correlation_id=correlation_id,
        )
        await self._backend.publish(channel, msg.to_dict())

        logger.debug("Request envoyée sur '%s' (corr: %s, timeout: %.1fs)",
                      channel, correlation_id, timeout)

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            logger.warning("Request timeout sur '%s' (corr: %s)", channel, correlation_id)
            raise
        finally:
            self._pending_requests.pop(correlation_id, None)

    async def reply(self, correlation_id: str, data: dict) -> None:
        """Envoie une réponse à un request précédent.

        Args:
            correlation_id: ID de corrélation du request original.
            data: Données de la réponse.
        """
        if correlation_id in self._pending_requests:
            future = self._pending_requests[correlation_id]
            if not future.done():
                future.set_result(data)
                logger.debug("Réponse envoyée pour corr: %s", correlation_id)
        else:
            logger.warning("Aucun request en attente pour corr: %s", correlation_id)

    # ── Broadcast ────────────────────────────────────────────────────────

    async def broadcast(
        self,
        message: dict,
        exclude: Optional[list[str]] = None,
    ) -> dict[str, str]:
        """Diffuse un message sur tous les canaux actifs.

        Args:
            message: Données à diffuser.
            exclude: Canaux à exclure de la diffusion.

        Returns:
            Dictionnaire {channel: message_id} des publications réussies.
        """
        exclude = set(exclude or [])
        results: dict[str, str] = {}

        for channel in VALID_CHANNELS:
            if channel in exclude:
                continue
            try:
                msg_id = await self.publish(channel, message)
                results[channel] = msg_id
            except Exception as exc:
                logger.warning("Broadcast échoué sur '%s': %s", channel, exc)

        logger.info("Broadcast envoyé à %d/%d canaux", len(results),
                     len(VALID_CHANNELS) - len(exclude))
        return results

    # ── Dead Letter Queue ────────────────────────────────────────────────

    async def _send_to_dlq(self, msg: NexusMessage, error: Exception) -> None:
        """Envoie un message en échec vers la Dead Letter Queue."""
        dlq_dir = self._file_backend._dlq_dir()
        entry = {
            "original_message": msg.to_dict(),
            "error": str(error),
            "failed_at": NexusMessage.now_iso(),
            "error_type": type(error).__name__,
        }
        filepath = dlq_dir / f"{msg.id}_dlq_{int(time.time())}.json"
        filepath.write_text(json.dumps(entry, ensure_ascii=False, indent=2))
        logger.warning("Message %s envoyé en DLQ: %s", msg.id, error)

    async def _retry_message(self, msg: NexusMessage) -> None:
        """Republic un message avec backoff exponentiel."""
        delay = RETRY_BASE_DELAY * (2 ** msg.retry_count)
        logger.info("Retry message %s dans %.1fs (tentative %d/%d)",
                     msg.id, delay, msg.retry_count + 1, MAX_RETRIES)
        await asyncio.sleep(delay)

        retry_msg = NexusMessage(
            id=uuid.uuid4().hex[:16],
            channel=msg.channel,
            data=msg.data,
            timestamp=NexusMessage.now_iso(),
            sender=msg.sender,
            retry_count=msg.retry_count,
        )
        try:
            await self._backend.publish(msg.channel, retry_msg.to_dict())
        except Exception as exc:
            logger.error("Retry échoué pour %s: %s", msg.id, exc)
            await self._send_to_dlq(msg, exc)

    async def _dlq_reprocess_loop(self) -> None:
        """Boucle de retraitement de la DLQ (toutes les 60s)."""
        while True:
            try:
                await asyncio.sleep(60)
                dlq_dir = self._file_backend._dlq_dir()
                if not dlq_dir.exists():
                    continue

                for fp in list(dlq_dir.glob("*_dlq_*.json")):
                    try:
                        entry = json.loads(fp.read_text())
                        original = entry.get("original_message", {})
                        retry_count = original.get("retry_count", 0)

                        # Ne pas retraiter si déjà trop de tentatives
                        if retry_count >= MAX_RETRIES:
                            continue

                        # Republie le message
                        msg = NexusMessage.from_dict(original)
                        msg.retry_count = retry_count + 1
                        await self._backend.publish(msg.channel, msg.to_dict())
                        fp.unlink()  # Supprime l'entrée DLQ
                        logger.info("DLQ retraité: %s", msg.id)
                    except Exception as exc:
                        logger.error("Erreur retraitement DLQ %s: %s", fp.name, exc)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Erreur boucle DLQ: %s", exc)
                await asyncio.sleep(30)

    # ── Heartbeat ────────────────────────────────────────────────────────

    async def send_heartbeat(self, module_name: Optional[str] = None) -> None:
        """Envoie un ping heartbeat pour signaler qu'un module est vivant."""
        name = module_name or self.sender_name
        self._heartbeats[name] = time.time()

        try:
            await self.publish("system", {
                "type": "heartbeat",
                "module": name,
                "timestamp": NexusMessage.now_iso(),
            })
        except Exception as exc:
            logger.debug("Heartbeat échoué pour %s: %s", name, exc)

    async def register_heartbeat(self, module_name: str) -> None:
        """Enregistre un module dans le registre des heartbeats."""
        self._heartbeats[module_name] = time.time()

    def get_alive_modules(self) -> dict[str, bool]:
        """Retourne le statut vivant/mort de chaque module enregistré."""
        now = time.time()
        return {
            name: (now - ts) < HEARTBEAT_TTL
            for name, ts in self._heartbeats.items()
        }

    async def _heartbeat_loop(self) -> None:
        """Boucle d'envoi automatique des heartbeats."""
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await self.send_heartbeat()
                # Nettoie les modules morts du registre
                alive = self.get_alive_modules()
                dead = [n for n, ok in alive.items() if not ok and n != self.sender_name]
                for name in dead:
                    logger.debug("Module '%s' considéré mort (pas de heartbeat depuis %ds)",
                                  name, HEARTBEAT_TTL)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Erreur boucle heartbeat: %s", exc)
                await asyncio.sleep(HEARTBEAT_INTERVAL)

    # ── Statut ───────────────────────────────────────────────────────────

    async def get_status(self) -> dict[str, Any]:
        """Retourne le statut global du bus."""
        status: dict[str, Any] = {
            "backend": self.backend_type,
            "channels": {},
            "alive_modules": self.get_alive_modules(),
            "pending_requests": len(self._pending_requests),
        }

        for channel in VALID_CHANNELS:
            channel_status: dict[str, Any] = {
                "consumers": list(self._consumers.get(channel, set())),
            }
            # Compteur de messages en attente
            for group_consumer in self._consumers.get(channel, set()):
                group, consumer = group_consumer.split("/", 1)
                pending = await self._backend.pending_count(channel, group)
                channel_status["pending"] = channel_status.get("pending", 0) + pending
            status["channels"][channel] = channel_status

        return status


# ─── API HTTP (FastAPI) ──────────────────────────────────────────────────────

def create_api(bus: NexusBus):
    """Crée l'application FastAPI pour le bus Nexus (port 9305)."""
    from fastapi import FastAPI, HTTPException, Body, Request
    from fastapi.responses import StreamingResponse
    from pydantic import BaseModel

    app = FastAPI(title="HERMES OMEGA - Nexus Bus API", version="1.0.0")

    # ── Modèles Pydantic ─────────────────────────────────────────────

    class PublishRequest(BaseModel):
        """Corps d'une requête de publication."""
        data: dict = {}
        sender: Optional[str] = None

    class RequestPayload(BaseModel):
        """Corps d'une requête request-response."""
        data: dict = {}
        sender: Optional[str] = None
        timeout: float = 30.0

    class ReplyPayload(BaseModel):
        """Corps d'une réponse à un request."""
        correlation_id: str
        data: dict = {}

    class BroadcastRequest(BaseModel):
        """Corps d'une requête de broadcast."""
        data: dict = {}
        exclude: list[str] = []

    class HeartbeatPayload(BaseModel):
        """Corps d'un heartbeat."""
        module: Optional[str] = None

    # ── Routes ───────────────────────────────────────────────────────

    @app.post("/publish/{channel}")
    async def publish_message(channel: str, payload: dict):
        """Publie un message sur un channel."""
        try:
            msg_id = await bus.publish(channel, payload)
            return {"status": "ok", "message_id": msg_id, "channel": channel}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/subscribe/{channel}")
    async def subscribe_stream(
        channel: str, group: str = "default", consumer: str = "api",
    ):
        """SSE stream pour consommer les messages d'un channel.

        Args:
            channel: Canal à écouter.
            group: Consumer group (défaut: "default").
            consumer: Nom du consumer (défaut: "api").

        Returns:
            Server-Sent Events stream avec les messages.
        """
        if channel not in VALID_CHANNELS:
            raise HTTPException(status_code=400, detail=f"Canal invalide: {channel}")

        async def event_generator():
            async for msg in bus.subscribe(channel, group, consumer):
                yield f"data: {json.dumps(msg.to_dict(), ensure_ascii=False)}\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/request/{channel}")
    async def request_message(channel: str, body: RequestPayload):
        """Pattern request-response sur un channel.

        Publie une requête et attend la réponse du module cible.

        Args:
            channel: Canal cible.
            body: Contenu de la requête (data, sender, timeout).

        Returns:
            La réponse du module cible ou une erreur timeout.
        """
        try:
            result = await bus.request(channel, body.data, timeout=body.timeout)
            return {"status": "ok", "response": result}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Request timeout")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/reply")
    async def reply_to_request(body: ReplyPayload):
        """Envoie une réponse à un request en attente.

        Args:
            body: correlation_id + data de la réponse.

        Returns:
            Statut de l'envoi.
        """
        await bus.reply(body.correlation_id, body.data)
        return {"status": "ok", "correlation_id": body.correlation_id}

    @app.post("/broadcast")
    async def broadcast_message(body: BroadcastRequest):
        """Diffuse un message sur tous les canaux actifs.

        Args:
            body: data + liste des canaux à exclure.

        Returns:
            Dictionnaire {channel: message_id} des publications.
        """
        results = await bus.broadcast(body.data, exclude=body.exclude)
        return {"status": "ok", "published": results}

    @app.get("/status")
    async def get_status():
        """Retourne le statut global du bus.

        Inclut: backend actif, canaux, consumers, modules vivants, requêtes en attente.
        """
        return await bus.get_status()

    @app.get("/channels")
    async def list_channels():
        """Liste les canaux valides du système."""
        return {
            "channels": sorted(VALID_CHANNELS),
            "active": {
                ch: list(bus._consumers.get(ch, set()))
                for ch in VALID_CHANNELS
                if ch in bus._consumers
            },
        }

    @app.post("/heartbeat")
    async def heartbeat(body: HeartbeatPayload):
        """Enregistre un heartbeat pour un module."""
        await bus.send_heartbeat(body.module)
        return {"status": "ok", "module": body.module or bus.sender_name}

    @app.get("/alive")
    async def alive_modules():
        """Retourne la liste des modules vivants."""
        return {"modules": bus.get_alive_modules()}

    return app


# ─── Point d'entrée principal ────────────────────────────────────────────────

async def run_server(sender_name: str = "nexus-api", host: str = "0.0.0.0"):
    """Démarre le serveur API Nexus Bus.

    Args:
        sender_name: Nom de l'émetteur pour les messages système.
        host: Adresse d'écoute (défaut: 0.0.0.0).
    """
    import uvicorn

    bus = NexusBus(sender_name=sender_name)
    await bus.start()

    app = create_api(bus)

    logger.info("Démarrage Nexus Bus API sur le port %d (backend: %s)",
                 API_PORT, bus.backend_type)

    config = uvicorn.Config(app, host=host, port=API_PORT, log_level="info")
    server = uvicorn.Server(config)

    try:
        await server.serve()
    finally:
        await bus.stop()


def main():
    """Point d'entrée CLI."""
    import uvicorn

    bus = NexusBus(sender_name="nexus-cli")
    app = create_api(bus)

    @app.on_event("startup")
    async def startup():
        await bus.start()

    @app.on_event("shutdown")
    async def shutdown():
        await bus.stop()

    uvicorn.run(app, host="0.0.0.0", port=API_PORT)


if __name__ == "__main__":
    main()
