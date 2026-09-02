"""Directive text and the replace_directive context transform."""

import pytest

from language.directives import SENTINEL, directive_for_band, replace_directive
from language.tracker import Band

_DEVANAGARI_BANDS = [Band.HINGLISH, Band.MOSTLY_HINDI]


@pytest.mark.parametrize("band", list(Band))
def test_every_band_has_a_sentinel_directive(band: Band):
    assert directive_for_band(band).startswith(SENTINEL)


@pytest.mark.parametrize("band", _DEVANAGARI_BANDS)
def test_hindi_bands_show_devanagari_examples(band: Band):
    """Few-shot lines must model Devanagari output for TTS pronunciation."""
    assert any("ऀ" <= ch <= "ॿ" for ch in directive_for_band(band))


def test_replace_directive_swaps_only_its_own_message():
    messages = [
        {"role": "system", "content": "persona"},
        {"role": "developer", "content": f"{SENTINEL} old directive"},
        {"role": "developer", "content": "[RAG] grounding block"},
        {"role": "user", "content": "hello"},
    ]
    result = replace_directive(Band.MOSTLY_HINDI)(messages)

    # New directive sits right after the system message; old one is gone;
    # everything else keeps its order.
    assert result[0] == messages[0]
    assert result[1] == {
        "role": "developer",
        "content": directive_for_band(Band.MOSTLY_HINDI),
    }
    assert result[2:] == [messages[2], messages[3]]
    assert sum(m["content"].startswith(SENTINEL) for m in result) == 1


def test_replace_directive_inserts_when_absent():
    result = replace_directive(Band.MOSTLY_ENGLISH)([{"role": "user", "content": "hey"}])
    assert len(result) == 2
    # No system message: the directive leads the context.
    assert result[0]["content"] == directive_for_band(Band.MOSTLY_ENGLISH)


def test_replace_directive_ignores_non_string_content():
    """Structured content (e.g. image parts) must pass through untouched."""
    weird = {"role": "developer", "content": [{"type": "text", "text": "x"}]}
    result = replace_directive(Band.HINGLISH)([weird])
    assert weird in result
    assert len(result) == 2
