"""
HERMES OMEGA - Knowledge Graph (Graphe de Connaissances Vectoriel)
=================================================================
Système de stockage et retrieval de connaissances vectorielles.

Backends (par ordre de préférence):
    1. Qdrant (localhost:6333) — stockage vectoriel haute performance
    2. Fichiers JSON locaux (~/.hermes-omega/knowledge/) — fallback avec TF-IDF

Embeddings (par ordre de préférence) :
    1. Ollama (localhost:11434/api/embeddings) — nomic-embed-text ou BGE-M3
    2. sentence-transformers — modèle local
    3. TF-IDF (scikit-learn) — dernier recours

Port API FastAPI : 9306
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Annotated

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Body, Request
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logger = logging.getLogger("hermes.omega.knowledge")

QDRANT_URL: str = os.getenv("QDRANT_URL", "http://localhost:6333")
OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL: str = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
API_PORT: int = int(os.getenv("KNOWLEDGE_API_PORT", "9306"))
DATA_DIR: Path = Path(os.getenv("HERMES_DATA_DIR", "~/.hermes-omega/knowledge")).expanduser()

# Dimensions par défaut selon le modèle d'embedding
EMBED_DIM_NOMIC: int = 768
EMBED_DIM_BGE_M3: int = 1024
EMBED_DIM_FALLBACK: int = 384

# Collections par défaut
DEFAULT_COLLECTIONS: list[str] = [
    "articles",
    "code",
    "decisions",
    "conversations",
    "entities",
]

# Modèles Pydantic pour l'API
class StoreRequest(BaseModel):
    """Requête de stockage d'un document."""
    content: str = Field(..., min_length=1, description="Contenu du document")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Métadonnées")
    ttl: Optional[int] = Field(default=None, description="Durée de vie en secondes")


class SearchRequest(BaseModel):
    """Requête de recherche sémantique."""
    query: str = Field(..., min_length=1, description="Requête de recherche")
    limit: int = Field(default=10, ge=1, le=100, description="Nombre max de résultats")
    threshold: float = Field(default=0.7, ge=0.0, le=1.0, description="Score minimal")


class BatchStoreRequest(BaseModel):
    """Requête de stockage en lot."""
    documents: list[StoreRequest] = Field(..., min_length=1)


class EmbedRequest(BaseModel):
    """Requête de génération d'embedding (debug)."""
    text: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def content_hash(text: str) -> str:
    """Calcule le hash SHA-256 d'un contenu pour déduplication."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def timestamp_now() -> float:
    """Timestamp UTC actuel en secondes."""
    return datetime.now(timezone.utc).timestamp()


def format_timestamp(ts: float) -> str:
    """Formate un timestamp en ISO 8601."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@dataclass
class Document:
    """Représentation interne d'un document stocké."""
    id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] = field(default_factory=list)
    created_at: float = field(default_factory=timestamp_now)
    ttl: Optional[int] = None

    @property
    def expires_at(self) -> Optional[float]:
        """Timestamp d'expiration, ou None si pas de TTL."""
        if self.ttl is None:
            return None
        return self.created_at + self.ttl

    @property
    def is_expired(self) -> bool:
        """Vérifie si le document a expiré."""
        if self.expires_at is None:
            return False
        return timestamp_now() > self.expires_at


# ---------------------------------------------------------------------------
# EmbeddingEngine — Génération d'embeddings
# ---------------------------------------------------------------------------

class EmbeddingEngine:
    """
    Moteur d'embeddings avec cascade de fallback :
        1. Ollama (nomic-embed-text / BGE-M3)
        2. sentence-transformers (local)
        3. TF-IDF (scikit-learn)
    """

    def __init__(
        self,
        ollama_url: str = OLLAMA_URL,
        model: str = OLLAMA_MODEL,
    ) -> None:
        self.ollama_url = ollama_url.rstrip("/")
        self.model = model
        self._available_backend: Optional[str] = None  # mise en cache au premier appel
        self._tfidf_vectorizer: Any = None
        self._tfidf_matrix: Any = None
        self._tfidf_docs: list[str] = []

    async def _embed_ollama(self, text: str) -> Optional[list[float]]:
        """Appelle l'API Ollama pour générer un embedding."""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                payload = {"model": self.model, "prompt": text}
                async with session.post(
                    f"{self.ollama_url}/api/embeddings",
                    json=payload,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("embedding")
                    logger.warning("Ollama a retourné le statut %d", resp.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.debug("Ollama indisponible : %s", exc)
        return None

    async def _embed_sentence_transformers(self, text: str) -> Optional[list[float]]:
        """Utilise sentence-transformers en local si disponible."""
        try:
            # Import paresseux — ne bloque pas si non installé
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer("all-MiniLM-L6-v2")
            embedding = model.encode(text, normalize_embeddings=True)
            return embedding.tolist()
        except ImportError:
            logger.debug("sentence-transformers non installé")
        except Exception as exc:
            logger.warning("Erreur sentence-transformers : %s", exc)
        return None

    def _tfidf_init(self) -> bool:
        """Initialise le vectoriseur TF-IDF si possible."""
        if self._tfidf_vectorizer is not None:
            return True
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.preprocessing import normalize

            self._tfidf_vectorizer = TfidfVectorizer(
                max_features=EMBED_DIM_FALLBACK,
                stop_words="english",
            )
            self._tfidf_normalize = normalize
            return True
        except ImportError:
            logger.warning("scikit-learn non installé — TF-IDF indisponible")
            return False

    def tfidf_update(self, texts: list[str]) -> None:
        """Met à jour la matrice TF-IDF avec de nouveaux textes."""
        if not self._tfidf_init():
            return
        self._tfidf_docs.extend(texts)
        if self._tfidf_docs:
            matrix = self._tfidf_vectorizer.fit_transform(self._tfidf_docs)
            self._tfidf_matrix = self._tfidf_normalize(matrix, norm="l2")

    def _embed_tfidf(self, text: str) -> list[float]:
        """Génère un embedding TF-IDF basique (synchrone)."""
        if not self._tfidf_init():
            return [0.0] * EMBED_DIM_FALLBACK
        # Fit si pas encore fait
        if self._tfidf_matrix is None:
            self._tfidf_update([text])
        vector = self._tfidf_vectorizer.transform([text])
        normalized = self._tfidf_normalize(vector, norm="l2")
        return normalized.toarray().flatten().tolist()

    async def embed(self, text: str) -> list[float]:
        """
        Génère un embedding pour le texte donné.
        Cascade : Ollama → sentence-transformers → TF-IDF.
        """
        # Premier appel : détection du backend disponible
        if self._available_backend is None:
            test = await self._embed_ollama("test")
            if test is not None:
                self._available_backend = "ollama"
                logger.info("Backend d'embedding : Ollama (%s)", self.model)
            else:
                test = await self._embed_sentence_transformers("test")
                if test is not None:
                    self._available_backend = "sentence-transformers"
                    logger.info("Backend d'embedding : sentence-transformers")
                else:
                    self._available_backend = "tfidf"
                    logger.info("Backend d'embedding : TF-IDF (fallback basique)")

        if self._available_backend == "ollama":
            result = await self._embed_ollama(text)
            if result is not None:
                return result
            # Fallback silencieux si Ollama devient indisponible
            logger.warning("Ollama indisponible, bascule sur TF-IDF pour ce texte")
            return self._embed_tfidf(text)
        elif self._available_backend == "sentence-transformers":
            result = await self._embed_sentence_transformers(text)
            if result is not None:
                return result
            return self._embed_tfidf(text)
        else:
            return self._embed_tfidf(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Génère des embeddings pour une liste de textes."""
        tasks = [self.embed(t) for t in texts]
        return await asyncio.gather(*tasks)

    @property
    def backend(self) -> Optional[str]:
        """Nom du backend actif."""
        return self._available_backend

    @property
    def dimension(self) -> int:
        """Dimension des vecteurs générés."""
        if self._available_backend == "ollama":
            return EMBED_DIM_NOMIC  # nomic-embed-text → 768
        elif self._available_backend == "sentence-transformers":
            return 384  # all-MiniLM-L6-v2
        return EMBED_DIM_FALLBACK


# ---------------------------------------------------------------------------
# KnowledgeStore — Stockage vectoriel
# ---------------------------------------------------------------------------

class KnowledgeStore:
    """
    Stockage vectoriel de connaissances avec Qdrant (préféré)
    et fallback sur fichiers JSON + TF-IDF.
    """

    def __init__(
        self,
        qdrant_url: str = QDRANT_URL,
        data_dir: Path = DATA_DIR,
        embedding_engine: Optional[EmbeddingEngine] = None,
    ) -> None:
        self.qdrant_url = qdrant_url.rstrip("/")
        self.data_dir = data_dir
        self.embedding_engine = embedding_engine or EmbeddingEngine()
        self._use_qdrant: Optional[bool] = None  # détecté au premier appel
        self._local_docs: dict[str, dict[str, Document]] = {}  # collection → {id → Document}

        # Crée le répertoire de données local
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # --- Détection du backend ---

    async def _check_qdrant(self) -> bool:
        """Vérifie si Qdrant est accessible."""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as s:
                async with s.get(f"{self.qdrant_url}/collections") as resp:
                    return resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False

    async def _ensure_backend(self) -> bool:
        """Détermine le backend à utiliser (Qdrant ou local)."""
        if self._use_qdrant is None:
            if await self._check_qdrant():
                self._use_qdrant = True
                logger.info("Backend de stockage : Qdrant (%s)", self.qdrant_url)
                await self._init_qdrant_collections()
            else:
                self._use_qdrant = False
                logger.info("Backend de stockage : fichiers locaux (%s)", self.data_dir)
                self._load_local_docs()
        return self._use_qdrant

    # --- Opérations Qdrant ---

    async def _init_qdrant_collections(self) -> None:
        """Crée les collections par défaut dans Qdrant si elles n'existent pas."""
        for name in DEFAULT_COLLECTIONS:
            try:
                async with aiohttp.ClientSession() as session:
                    # Vérifie si la collection existe
                    async with session.get(
                        f"{self.qdrant_url}/collections/{name}"
                    ) as resp:
                        if resp.status == 200:
                            continue  # déjà existante

                    # Crée la collection
                    dim = self.embedding_engine.dimension
                    payload = {
                        "vectors": {"size": dim, "distance": "Cosine"},
                    }
                    async with session.put(
                        f"{self.qdrant_url}/collections/{name}",
                        json=payload,
                    ) as resp:
                        if resp.status in (200, 201):
                            logger.info("Collection Qdrant créée : %s (dim=%d)", name, dim)
                        else:
                            text = await resp.text()
                            logger.warning("Erreur création collection %s : %s", name, text)
            except Exception as exc:
                logger.error("Erreur init collection %s : %s", name, exc)

    async def _qdrant_store(
        self, collection: str, doc: Document
    ) -> str:
        """Stocke un document dans Qdrant."""
        # S'assure que la collection existe
        dim = self.embedding_engine.dimension
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.qdrant_url}/collections/{collection}") as r:
                if r.status != 200:
                    payload = {"vectors": {"size": dim, "distance": "Cosine"}}
                    await session.put(
                        f"{self.qdrant_url}/collections/{collection}", json=payload
                    )

            # Upsert du point
            point = {
                "id": doc.id,
                "vector": doc.embedding,
                "payload": {
                    "content": doc.content,
                    "metadata": doc.metadata,
                    "content_hash": content_hash(doc.content),
                    "created_at": doc.created_at,
                    "ttl": doc.ttl,
                },
            }
            async with session.put(
                f"{self.qdrant_url}/collections/{collection}/points",
                json={"points": [point]},
            ) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise RuntimeError(f"Qdrant upsert échoué : {text}")
        return doc.id

    async def _qdrant_search(
        self, collection: str, query_vector: list[float], limit: int, threshold: float
    ) -> list[dict]:
        """Recherche dans Qdrant par similarité."""
        async with aiohttp.ClientSession() as session:
            payload = {
                "vector": query_vector,
                "limit": limit,
                "score_threshold": threshold,
                "with_payload": True,
            }
            async with session.post(
                f"{self.qdrant_url}/collections/{collection}/points/search",
                json=payload,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Qdrant search échoué : {text}")
                data = await resp.json()

        results = []
        now = timestamp_now()
        for hit in data.get("result", []):
            pl = hit.get("payload", {})
            # Vérifie le TTL
            ttl = pl.get("ttl")
            created = pl.get("created_at", now)
            if ttl and (now - created > ttl):
                continue  # document expiré
            results.append({
                "id": hit["id"],
                "content": pl.get("content", ""),
                "metadata": pl.get("metadata", {}),
                "score": hit.get("score", 0.0),
                "created_at": format_timestamp(created) if created else None,
            })
        return results

    async def _qdrant_delete(self, collection: str, doc_id: str) -> None:
        """Supprime un point de Qdrant."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.qdrant_url}/collections/{collection}/points/delete",
                json={"points": [doc_id]},
            ) as resp:
                if resp.status not in (200, 204):
                    text = await resp.text()
                    logger.warning("Suppression Qdrant échouée : %s", text)

    async def _qdrant_collections(self) -> list[str]:
        """Liste les collections Qdrant."""
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.qdrant_url}/collections") as resp:
                data = await resp.json()
                return [c["name"] for c in data.get("result", {}).get("collections", [])]

    async def _qdrant_stats(self) -> dict[str, dict]:
        """Statistiques par collection dans Qdrant."""
        collections = await self._qdrant_collections()
        stats = {}
        async with aiohttp.ClientSession() as session:
            for name in collections:
                try:
                    async with session.get(
                        f"{self.qdrant_url}/collections/{name}"
                    ) as resp:
                        data = await resp.json()
                        info = data.get("result", {})
                        stats[name] = {
                            "count": info.get("points_count", 0),
                            "size": info.get("vectors_count", 0),
                            "status": info.get("status", "unknown"),
                        }
                except Exception as exc:
                    stats[name] = {"error": str(exc)}
        return stats

    # --- Opérations locales (fichiers JSON) ---

    def _collection_dir(self, collection: str) -> Path:
        """Répertoire d'une collection locale."""
        return self.data_dir / collection

    def _doc_path(self, collection: str, doc_id: str) -> Path:
        """Chemin d'un document local."""
        return self._collection_dir(collection) / f"{doc_id}.json"

    def _load_local_docs(self) -> None:
        """Charge tous les documents locaux en mémoire."""
        for col_dir in self.data_dir.iterdir():
            if not col_dir.is_dir():
                continue
            collection = col_dir.name
            self._local_docs.setdefault(collection, {})
            for json_file in col_dir.glob("*.json"):
                try:
                    data = json.loads(json_file.read_text(encoding="utf-8"))
                    doc = Document(
                        id=data["id"],
                        content=data["content"],
                        metadata=data.get("metadata", {}),
                        embedding=data.get("embedding", []),
                        created_at=data.get("created_at", 0),
                        ttl=data.get("ttl"),
                    )
                    # Ignore les documents expirés
                    if not doc.is_expired:
                        self._local_docs[collection][doc.id] = doc
                except Exception as exc:
                    logger.warning("Erreur chargement %s : %s", json_file, exc)

        # Met à jour TF-IDF si c'est le backend actif
        all_texts = [
            doc.content
            for docs in self._local_docs.values()
            for doc in docs.values()
        ]
        if all_texts:
            self.embedding_engine.tfidf_update(all_texts)

    def _save_local_doc(self, collection: str, doc: Document) -> None:
        """Sauvegarde un document local en JSON."""
        col_dir = self._collection_dir(collection)
        col_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "id": doc.id,
            "content": doc.content,
            "metadata": doc.metadata,
            "embedding": doc.embedding,
            "content_hash": content_hash(doc.content),
            "created_at": doc.created_at,
            "ttl": doc.ttl,
        }
        self._doc_path(collection, doc.id).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _delete_local_doc(self, collection: str, doc_id: str) -> None:
        """Supprime un document local."""
        path = self._doc_path(collection, doc_id)
        if path.exists():
            path.unlink()
        self._local_docs.get(collection, {}).pop(doc_id, None)

    def _local_search(
        self, collection: str, query: str, limit: int, threshold: float
    ) -> list[dict]:
        """Recherche locale par similarité cosinus."""
        docs = self._local_docs.get(collection, {})
        if not docs:
            return []

        # Calcule l'embedding de la requête
        loop = asyncio.get_event_loop()
        query_vec = loop.run_until_complete(self.embedding_engine.embed(query))

        results = []
        for doc in docs.values():
            if doc.is_expired:
                continue
            if not doc.embedding:
                continue
            # Similarité cosinus
            score = self._cosine_similarity(query_vec, doc.embedding)
            if score >= threshold:
                results.append({
                    "id": doc.id,
                    "content": doc.content,
                    "metadata": doc.metadata,
                    "score": round(score, 4),
                    "created_at": format_timestamp(doc.created_at),
                })

        # Tri par score décroissant
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    def _local_get_collections(self) -> list[str]:
        """Liste les collections locales."""
        return [
            d.name
            for d in self.data_dir.iterdir()
            if d.is_dir()
        ]

    def _local_get_stats(self) -> dict[str, dict]:
        """Statistiques des collections locales."""
        stats = {}
        for col_name, docs in self._local_docs.items():
            active = sum(1 for d in docs.values() if not d.is_expired)
            col_dir = self._collection_dir(col_name)
            total_size = sum(f.stat().st_size for f in col_dir.glob("*.json"))
            last_ts = max(
                (d.created_at for d in docs.values()),
                default=0.0,
            )
            stats[col_name] = {
                "count": active,
                "size": total_size,
                "last_updated": format_timestamp(last_ts) if last_ts else None,
            }
        return stats

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Calcule la similarité cosinus entre deux vecteurs."""
        if len(a) != len(b) or not a:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    # --- Déduplication ---

    async def _find_duplicate(
        self, collection: str, content: str
    ) -> Optional[str]:
        """Cherche un document existant avec le même contenu (hash)."""
        h = content_hash(content)
        await self._ensure_backend()
        if self._use_qdrant:
            # Requête Qdrant par filtrage du content_hash
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.qdrant_url}/collections/{collection}/points/scroll",
                    json={"limit": 1000, "with_payload": True},
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for point in data.get("result", {}).get("points", []):
                            if point.get("payload", {}).get("content_hash") == h:
                                return str(point["id"])
        else:
            # Recherche locale
            for doc_id, doc in self._local_docs.get(collection, {}).items():
                if doc.metadata.get("content_hash") == h:
                    return doc_id
        return None

    # --- API publique ---

    async def store(
        self,
        collection: str,
        content: str,
        metadata: Optional[dict] = None,
        ttl: Optional[int] = None,
    ) -> str:
        """
        Stocke un document dans la collection.
        Retourne l'ID du document (hash SHA-256 tronqué).
        Déduplication : si un document identique existe, retourne son ID.
        """
        # Vérifie la déduplication
        existing = await self._find_duplicate(collection, content)
        if existing:
            logger.debug("Document dupliqué ignoré : %s", existing)
            return existing

        await self._ensure_backend()
        doc_id = content_hash(content)[:16]  # Hash hex
        # Convertir en UUID pour Qdrant (format 8-4-4-4-12)
        doc_id = str(uuid.UUID(doc_id.ljust(32, '0')))

        # Génération de l'embedding avec fallback silencieux
        try:
            embedding = await self.embedding_engine.embed(content)
            if not embedding:
                raise ValueError("Embedding vide retourné par le backend")
        except Exception as exc:
            # Fallback silencieux : vecteur de zéros (dimension 384)
            logger.warning(
                "Embedding échoué pour %s/%s (%s), utilisation d'un vecteur de zéros (dim=384)",
                collection, doc_id[:8], exc,
            )
            embedding = [0.0] * EMBED_DIM_FALLBACK

        doc = Document(
            id=doc_id,
            content=content,
            metadata=metadata or {},
            embedding=embedding,
            ttl=ttl,
        )

        if self._use_qdrant:
            await self._qdrant_store(collection, doc)
        else:
            self._local_docs.setdefault(collection, {})[doc_id] = doc
            self._save_local_doc(collection, doc)
            # Met à jour TF-IDF
            self.embedding_engine.tfidf_update([content])

        logger.info("Document stocké : %s/%s", collection, doc_id)
        return doc_id

    async def batch_store(
        self, collection: str, documents: list[dict]
    ) -> list[str]:
        """
        Stockage en lot pour ingestion massive.
        Chaque élément : {"content": str, "metadata": dict, "ttl": int|None}
        Retourne la liste des IDs.
        """
        ids = []
        for doc_data in documents:
            doc_id = await self.store(
                collection=collection,
                content=doc_data["content"],
                metadata=doc_data.get("metadata"),
                ttl=doc_data.get("ttl"),
            )
            ids.append(doc_id)
        logger.info("Batch store : %d documents dans %s", len(ids), collection)
        return ids

    async def search(
        self,
        collection: str,
        query: str,
        limit: int = 10,
        threshold: float = 0.7,
    ) -> list[dict]:
        """
        Recherche sémantique dans une collection.
        Retourne une liste de résultats triés par score décroissant.
        """
        await self._ensure_backend()
        query_vector = await self.embedding_engine.embed(query)

        if self._use_qdrant:
            return await self._qdrant_search(
                collection, query_vector, limit, threshold
            )
        else:
            return self._local_search(collection, query, limit, threshold)

    async def delete(self, collection: str, doc_id: str) -> None:
        """Supprime un document d'une collection."""
        await self._ensure_backend()
        if self._use_qdrant:
            await self._qdrant_delete(collection, doc_id)
        else:
            self._delete_local_doc(collection, doc_id)
        logger.info("Document supprimé : %s/%s", collection, doc_id)

    async def get_collections(self) -> list[str]:
        """Liste toutes les collections."""
        await self._ensure_backend()
        if self._use_qdrant:
            return await self._qdrant_collections()
        return self._local_get_collections()

    async def get_stats(self) -> dict[str, dict]:
        """Statistiques par collection."""
        await self._ensure_backend()
        if self._use_qdrant:
            return await self._qdrant_stats()
        return self._local_get_stats()


# ---------------------------------------------------------------------------
# KnowledgeAPI — Serveur FastAPI (port 9306)
# ---------------------------------------------------------------------------

def create_app(store: Optional[KnowledgeStore] = None) -> FastAPI:
    """Crée et configure l'application FastAPI."""
    app = FastAPI(
        title="HERMES OMEGA — Knowledge Graph",
        description="API de stockage et retrieval de connaissances vectorielles",
        version="1.0.0",
    )

    _store: KnowledgeStore = store or KnowledgeStore()
    _embedding: EmbeddingEngine = _store.embedding_engine

    # --- Endpoints ---

    @app.post("/store/{collection}", response_model=dict)
    async def api_store(collection: str, payload: dict) -> dict:
        """Stocke un document dans une collection."""
        try:
            doc_id = await _store.store(
                collection=collection,
                content=payload.get("content", ""),
                metadata=payload.get("metadata"),
                ttl=payload.get("ttl"),
            )
            return {"status": "ok", "document_id": doc_id, "collection": collection}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/store/{collection}/batch", response_model=dict)
    async def api_batch_store(collection: str, payload: dict) -> dict:
        """Stockage en lot."""
        try:
            documents = payload.get("documents", [])
            ids = await _store.batch_store(collection, documents)
            return {"status": "ok", "count": len(ids), "document_ids": ids}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/search/{collection}", response_model=dict)
    async def api_search(collection: str, payload: dict) -> dict:
        """Recherche sémantique dans une collection."""
        try:
            results = await _store.search(
                collection=collection,
                query=payload.get("query", ""),
                limit=payload.get("limit", 10),
                threshold=payload.get("threshold", 0.0),
            )
            return {
                "status": "ok",
                "query": payload.get("query", ""),
                "count": len(results),
                "results": results,
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.delete("/document/{collection}/{doc_id}", response_model=dict)
    async def api_delete(collection: str, doc_id: str) -> dict:
        """Supprime un document."""
        await _store.delete(collection, doc_id)
        return {"status": "ok", "deleted": doc_id}

    @app.get("/collections", response_model=dict)
    async def api_collections() -> dict:
        """Liste toutes les collections."""
        collections = await _store.get_collections()
        return {"status": "ok", "collections": collections}

    @app.get("/stats", response_model=dict)
    async def api_stats() -> dict:
        """Statistiques détaillées par collection."""
        stats = await _store.get_stats()
        return {"status": "ok", "stats": stats, "backend": "qdrant" if _store._use_qdrant else "local"}

    @app.post("/embed", response_model=dict)
    async def api_embed(body: EmbedRequest) -> dict:
        """Génère un embedding (endpoint de debug)."""
        vector = await _embedding.embed(body.text)
        return {
            "status": "ok",
            "backend": _embedding.backend,
            "dimension": len(vector),
            "embedding_preview": vector[:5],  # 5 premières valeurs pour debug
        }

    @app.get("/health", response_model=dict)
    async def api_health() -> dict:
        """Vérification de santé du service."""
        qdrant_ok = await _store._check_qdrant()
        return {
            "health": "ok",
            "qdrant": qdrant_ok,
            "embedding_backend": _embedding.backend,
        }

    return app


# ---------------------------------------------------------------------------
# Point d'entrée principal
# ---------------------------------------------------------------------------

def main() -> None:
    """Lance le serveur API HERMES OMEGA Knowledge Graph."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Démarrage HERMES OMEGA Knowledge Graph sur le port %d", API_PORT)
    logger.info("  Qdrant URL  : %s", QDRANT_URL)
    logger.info("  Ollama URL  : %s", OLLAMA_URL)
    logger.info("  Data dir    : %s", DATA_DIR)

    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=API_PORT)


if __name__ == "__main__":
    main()
