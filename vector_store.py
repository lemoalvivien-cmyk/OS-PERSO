"""
Semantic Vector Store using ChromaDB (local, free, no SaaS).
Provides: F60 Vector Store + F61 Embeddings in one module.
Embeddings: uses sentence-transformers (all-MiniLM-L6-v2) - runs fully local.
Install: pip install chromadb sentence-transformers
"""
import sys, os, json
from pathlib import Path
from datetime import datetime

_LOG = []
_DATA_DIR = Path(r"C:\OS_INTERNE\data\vectorstore")
_COLLECTION = "autoclaw_memory"

def _log(action, **kw):
    entry = {"ts": datetime.now().isoformat(), "action": action, **kw}
    _LOG.append(entry)

def _get_client():
    """Get or create ChromaDB client."""
    try:
        import chromadb
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(_DATA_DIR))
        return client
    except ImportError:
        return None

def _get_collection(name=_COLLECTION):
    """Get or create a collection."""
    client = _get_client()
    if not client:
        return None, "chromadb not installed"
    try:
        collection = client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"}
        )
        return collection, None
    except Exception as e:
        return None, str(e)

def index_entry(entry_id: str, text: str, metadata: dict = None) -> dict:
    """Index a text entry for semantic search."""
    collection, err = _get_collection()
    if err:
        return {"ok": False, "error": err, "install": "pip install chromadb sentence-transformers"}
    
    try:
        collection.upsert(
            ids=[entry_id],
            documents=[text],
            metadatas=[metadata or {}]
        )
        _log("indexed", id=entry_id, chars=len(text))
        return {"ok": True, "id": entry_id}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}

def search(query: str, n: int = 5, filter_meta: dict = None) -> dict:
    """Semantic search over indexed entries."""
    collection, err = _get_collection()
    if err:
        return {"ok": False, "error": err, "install": "pip install chromadb sentence-transformers"}
    
    try:
        results = collection.query(
            query_texts=[query],
            n_results=n,
            where=filter_meta or None
        )
        docs = results.get("documents", [[]])[0]
        ids = results.get("ids", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]
        
        items = []
        for i, doc in enumerate(docs):
            items.append({
                "id": ids[i] if i < len(ids) else "",
                "text": doc,
                "metadata": metas[i] if i < len(metas) else {},
                "distance": round(distances[i], 4) if i < len(distances) else 0
            })
        
        return {"ok": True, "results": items, "query": query}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}

def delete_entry(entry_id: str) -> dict:
    """Delete an indexed entry."""
    collection, err = _get_collection()
    if err:
        return {"ok": False, "error": err}
    try:
        collection.delete(ids=[entry_id])
        return {"ok": True, "deleted": entry_id}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}

def count_entries() -> dict:
    """Count total indexed entries."""
    collection, err = _get_collection()
    if err:
        return {"ok": False, "error": err}
    try:
        n = collection.count()
        return {"ok": True, "count": n}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}

def list_collections() -> dict:
    """List all collections."""
    client = _get_client()
    if not client:
        return {"ok": False, "error": "chromadb not installed"}
    try:
        cols = [c.name for c in client.list_collections()]
        return {"ok": True, "collections": cols}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}

def status():
    """Check if vector store is operational."""
    client = _get_client()
    if not client:
        return {
            "ok": False,
            "status": "needs_install",
            "install": "pip install chromadb sentence-transformers",
            "note": "Fully local, no SaaS, no API key needed"
        }
    
    # Check if default embedding model works
    try:
        import chromadb
        # Test with a tiny operation
        col, err = _get_collection("_health_check")
        if err:
            return {"ok": False, "status": "error", "error": err}
        col.delete() if col else None
        return {
            "ok": True,
            "status": "ready",
            "backend": "chromadb",
            "data_dir": str(_DATA_DIR),
            "default_collection": _COLLECTION
        }
    except Exception as e:
        return {"ok": False, "status": "error", "error": str(e)[:200]}

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(status(), indent=2))
