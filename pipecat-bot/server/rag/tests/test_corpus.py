"""Corpus hygiene + (slow) real-model threshold separation regression."""

import json
from pathlib import Path

import pytest

CORPUS = Path(__file__).parent.parent / "corpus"


@pytest.fixture(scope="module")
def qa() -> dict:
    return json.loads((CORPUS / "qa.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def traps() -> dict:
    return json.loads((CORPUS / "unanswerable.json").read_text(encoding="utf-8"))


def test_corpus_size(qa: dict, traps: dict):
    assert 50 <= len(qa["entries"]) <= 70
    assert 8 <= len(traps["entries"]) <= 12


def test_ids_unique(qa: dict, traps: dict):
    ids = [e["id"] for e in qa["entries"]] + [t["id"] for t in traps["entries"]]
    assert len(ids) == len(set(ids))


def test_every_entry_has_cross_script_paraphrases(qa: dict):
    for e in qa["entries"]:
        assert len(e["paraphrases"]) >= 2, e["id"]
        # At least one paraphrase carries Devanagari (cross-script retrieval).
        assert any(any("ऀ" <= ch <= "ॿ" for ch in p) for p in e["paraphrases"]), e["id"]


def test_answers_are_tts_safe(qa: dict):
    for e in qa["entries"]:
        answer = e["answer"]
        assert answer.strip(), e["id"]
        for bad in ("*", "#", "•", "\n-", "http"):
            assert bad not in answer, f"{e['id']} answer contains {bad!r}"


def test_entries_carry_known_sources(qa: dict):
    sources = qa["sources"]
    for e in qa["entries"]:
        assert e["source"] in sources, e["id"]


@pytest.mark.slow
def test_threshold_separation_with_real_model(qa: dict, traps: dict):
    """No trap question may ever reach the HIGH ("answer strictly") band.

    This is the anti-fabrication invariant the committed thresholds encode:
    entity overlap can push a trap into MID (whose instruction allows the
    model to deflect, verified by the evals), but never into HIGH, where the
    model is told to answer without further judgment. Keeps corpus edits
    honest — a new entry that pulls a trap above HIGH fails here.

    Slow: loads the real ONNX embedding model (downloads ~220MB first run).
    """
    from rag.embedder import embed
    from rag.gate import _thresholds
    from rag.index import QAIndex

    index = QAIndex.load(CORPUS / "qa.json", embed_fn=embed)
    high, floor = _thresholds()[0], _thresholds()[1]
    assert floor < high

    for t in traps["entries"]:
        for probe in [t["question"], *t.get("paraphrases", [])]:
            top = index.search(index.embed_query(probe), top_k=1)[0]
            assert top[1] < high, (
                f"trap {t['id']} probe {probe!r} scored {top[1]:.3f} >= HIGH "
                f"({high}) against {top[0].id} — it would be answered as fact"
            )

    # Sanity: every entry's own question routes to itself with a clean HIGH.
    for e in qa["entries"]:
        top = index.search(index.embed_query(e["question"]), top_k=1)[0]
        assert top[0].id == e["id"], f"{e['id']} misrouted to {top[0].id}"
        assert top[1] >= high, f"{e['id']} own question scored {top[1]:.3f}"
