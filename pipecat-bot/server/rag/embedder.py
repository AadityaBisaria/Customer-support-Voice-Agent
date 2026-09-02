"""fastembed wrapper: one shared ONNX embedding model per process.

The model is multilingual (Devanagari + Latin), ~0.22 GB, loaded lazily on
first use and cached for the life of the process — the eval transport reuses
the process across sessions, so repeat sessions pay nothing.
"""

import os
from functools import lru_cache

import numpy as np

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


@lru_cache(maxsize=1)
def get_embedder():
    from fastembed import TextEmbedding

    model_name = os.getenv("RAG_EMBED_MODEL", DEFAULT_MODEL)
    return TextEmbedding(model_name)


def embed(texts: list[str]) -> np.ndarray:
    """Embed texts to L2-normalized float32 rows (so dot product = cosine)."""
    vectors = np.array(list(get_embedder().embed(texts)), dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms
