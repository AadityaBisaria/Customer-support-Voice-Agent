"""The three-way gate: band boundaries, message content, context transform."""

import pytest

from rag.gate import SENTINEL, GateBand, decide, grounding_message, replace_grounding
from rag.index import QAEntry

ENTRY = QAEntry(id="qa-x", question="refund timeline?", answer="Up to 5 working days.")
OTHER = QAEntry(id="qa-y", question="return window?", answer="Ten days.")


@pytest.mark.parametrize(
    "score, band",
    [
        (0.95, GateBand.HIGH),
        (0.80, GateBand.HIGH),  # boundary: >= high
        (0.79, GateBand.MID),
        (0.55, GateBand.MID),  # boundary: >= floor
        (0.54, GateBand.FLOOR),
        (0.10, GateBand.FLOOR),
    ],
)
def test_band_boundaries(score: float, band: GateBand):
    decision = decide([(ENTRY, score)], high=0.80, floor=0.55)
    assert decision.band is band
    assert decision.top_score == pytest.approx(score)


def test_no_matches_is_floor():
    assert decide([], high=0.8, floor=0.55).band is GateBand.FLOOR


def test_high_message_contains_only_the_top_answer():
    decision = decide([(ENTRY, 0.9), (OTHER, 0.85)], high=0.8, floor=0.55)
    message = grounding_message(decision)
    assert message.startswith(SENTINEL)
    assert ENTRY.answer in message
    assert OTHER.answer not in message
    assert "strictly" in message


def test_mid_message_lists_candidates_and_allows_deflection():
    decision = decide([(ENTRY, 0.7), (OTHER, 0.6)], high=0.8, floor=0.55)
    message = grounding_message(decision)
    assert ENTRY.answer in message and OTHER.answer in message
    assert "don't have that information" in message


def test_floor_message_forbids_invention_and_carries_no_answers():
    decision = decide([(ENTRY, 0.2)], high=0.8, floor=0.55)
    message = grounding_message(decision)
    assert "NO answer" in message
    assert "Do NOT invent" in message
    assert ENTRY.answer not in message


def test_replace_grounding_swaps_only_its_own_message():
    decision = decide([(ENTRY, 0.9)], high=0.8, floor=0.55)
    messages = [
        {"role": "system", "content": "persona"},
        {"role": "developer", "content": f"{SENTINEL} stale grounding"},
        {"role": "developer", "content": "[LANG-STYLE] directive"},
        {"role": "user", "content": "refund kab milega"},
    ]
    result = replace_grounding(decision)(messages)
    assert result[:3] == [messages[0], messages[2], messages[3]]
    assert result[-1]["content"] == grounding_message(decision)
    assert sum(m["content"].startswith(SENTINEL) for m in result) == 1


def test_env_defaults_are_used_when_not_passed(monkeypatch):
    monkeypatch.setenv("RAG_THRESHOLD_HIGH", "0.9")
    monkeypatch.setenv("RAG_THRESHOLD_FLOOR", "0.3")
    assert decide([(ENTRY, 0.85)]).band is GateBand.MID
    assert decide([(ENTRY, 0.25)]).band is GateBand.FLOOR
