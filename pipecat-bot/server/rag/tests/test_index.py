"""QAIndex: loading, row->entry mapping, ranking, dedupe. Uses a fake embedder."""

import numpy as np
import pytest

from rag.index import QAEntry, QAIndex

_VOCAB = ["refund", "return", "replace", "gift", "card", "delivery", "missing", "emi"]


def fake_embed(texts: list[str]) -> np.ndarray:
    """Bag-of-words over a tiny vocab, L2-normalized: cosine = token overlap."""
    rows = []
    for text in texts:
        tokens = text.lower().split()
        vec = np.array([float(tokens.count(w)) for w in _VOCAB], dtype=np.float32)
        norm = np.linalg.norm(vec)
        rows.append(vec / norm if norm else vec)
    return np.array(rows, dtype=np.float32)


@pytest.fixture()
def index() -> QAIndex:
    entries = [
        QAEntry(
            id="qa-refund",
            question="refund timeline",
            answer="Refunds take days.",
            paraphrases=("refund kab milega", "refund delivery"),
        ),
        QAEntry(id="qa-gift", question="gift card return", answer="Gift cards final."),
        QAEntry(id="qa-emi", question="emi refund", answer="EMI is refunded."),
    ]
    return QAIndex(entries, embed_fn=fake_embed)


def test_search_ranks_by_similarity(index: QAIndex):
    results = index.search(index.embed_query("gift card"), top_k=3)
    assert results[0][0].id == "qa-gift"
    # 2 shared tokens of 2 (query) x 3 (entry): cos = 2 / (sqrt(2) * sqrt(3))
    assert results[0][1] == pytest.approx(2 / (2**0.5 * 3**0.5), abs=1e-5)


def test_paraphrase_rows_map_back_to_their_entry(index: QAIndex):
    # "refund delivery" only exists as a paraphrase of qa-refund.
    results = index.search(index.embed_query("refund delivery"), top_k=1)
    assert results[0][0].id == "qa-refund"


def test_entries_are_deduped_keeping_best_score(index: QAIndex):
    # "refund" matches qa-refund via question AND both paraphrases: one result.
    results = index.search(index.embed_query("refund"), top_k=3)
    ids = [entry.id for entry, _ in results]
    assert ids.count("qa-refund") == 1


def test_top_k_limits_results(index: QAIndex):
    assert len(index.search(index.embed_query("refund emi gift"), top_k=2)) == 2


def test_empty_query_overlap_scores_zero(index: QAIndex):
    results = index.search(index.embed_query("unrelated words entirely"), top_k=1)
    assert results[0][1] == pytest.approx(0.0, abs=1e-6)
