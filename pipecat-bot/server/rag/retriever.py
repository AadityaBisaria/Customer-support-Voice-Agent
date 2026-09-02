"""The Retriever Protocol — the RAG production seam.

`QAIndex` (in-memory numpy cosine over a small corpus) conforms today. At
production document scale, implement the same Protocol over a vector store
(Qdrant, pgvector, ...) and add chunked-document ingestion; the processor,
the gate, and the pipeline wiring don't change. `QAIndex.embed_fn` is already
injectable, so the embedding model swaps independently of the search backend.
"""

from typing import Protocol, runtime_checkable

import numpy as np

from .index import QAEntry


@runtime_checkable
class Retriever(Protocol):
    def embed_query(self, text: str) -> np.ndarray: ...

    def search(self, query_vec: np.ndarray, top_k: int = 3) -> list[tuple[QAEntry, float]]: ...
