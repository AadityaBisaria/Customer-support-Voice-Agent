"""The Q&A index: load the corpus, embed question + paraphrases, cosine search.

Each entry contributes several index rows (its question and each paraphrase),
all mapping back to the same entry — the cross-script paraphrases are what
let a romanized-Hinglish or Devanagari query land on an English Q&A. Search
dedupes rows back to entries, keeping each entry's best score.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class QAEntry:
    id: str
    question: str
    answer: str
    paraphrases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    source_url: str = ""


class QAIndex:
    """In-memory cosine index over a few hundred vectors. No vector DB."""

    def __init__(
        self,
        entries: list[QAEntry],
        *,
        embed_fn: Callable[[list[str]], np.ndarray] | None = None,
    ) -> None:
        if embed_fn is None:
            from .embedder import embed as embed_fn  # deferred: heavy ONNX load

        self._embed_fn = embed_fn
        self.entries = list(entries)

        texts: list[str] = []
        self._row_entry: list[QAEntry] = []
        for entry in self.entries:
            for text in (entry.question, *entry.paraphrases):
                texts.append(text)
                self._row_entry.append(entry)
        self._vectors = self._embed_fn(texts) if texts else np.zeros((0, 1), np.float32)

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        embed_fn: Callable[[list[str]], np.ndarray] | None = None,
    ) -> "QAIndex":
        data = json.loads(path.read_text(encoding="utf-8"))
        sources = data.get("sources", {})
        entries = [
            QAEntry(
                id=e["id"],
                question=e["question"],
                answer=e["answer"],
                paraphrases=tuple(e.get("paraphrases", ())),
                tags=tuple(e.get("tags", ())),
                source_url=sources.get(e.get("source", ""), e.get("source_url", "")),
            )
            for e in data["entries"]
        ]
        return cls(entries, embed_fn=embed_fn)

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed_fn([text])[0]

    def search(self, query_vec: np.ndarray, top_k: int = 3) -> list[tuple[QAEntry, float]]:
        """Top entries by cosine similarity, deduped (best row per entry)."""
        if not len(self._vectors):
            return []
        scores = self._vectors @ query_vec

        best: dict[str, tuple[QAEntry, float]] = {}
        for row, score in enumerate(scores):
            entry = self._row_entry[row]
            kept = best.get(entry.id)
            if kept is None or score > kept[1]:
                best[entry.id] = (entry, float(score))

        ranked = sorted(best.values(), key=lambda pair: pair[1], reverse=True)
        return ranked[:top_k]
