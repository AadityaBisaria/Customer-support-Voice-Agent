"""YesNoSlot: consent tables in both scripts; backchannels are not consent."""

from datetime import UTC, datetime, timezone

import pytest

from dialogue.results import Ambiguous, Fit, NoFit
from dialogue.slots import FitContext
from dialogue.utterance import Utterance
from dialogue.yes_no import YesNoSlot

SLOT = YesNoSlot(name="confirmed", prompt="haan ya nahi?")
CTX = FitContext(now=datetime(2026, 9, 2, 12, 0, tzinfo=UTC))


async def fit(said: str):
    return await SLOT.fit_utterance(Utterance.from_text(said), CTX)


@pytest.mark.parametrize(
    "said",
    [
        "haan",
        "ji haan",
        "haan kar do",
        "bilkul",
        "theek hai",
        "kar do",
        "yes please",
        "sure go ahead",
        "confirm karo",
        "ok karo",
        "हां",
        "जी हाँ",
        "ठीक है",
        "कर दो",
        "बिल्कुल",
        "हाँ कर दो",
    ],
)
async def test_yes(said: str):
    result = await fit(said)
    assert isinstance(result, Fit) and result.value is True, said


@pytest.mark.parametrize(
    "said",
    [
        "nahi",
        "nahin",
        "mat karo",
        "rehne do",
        "cancel kar do",
        "ruko",
        "no no",
        "abhi nahi",
        "नहीं",
        "मत करो",
        "रहने दो",
        "रुको",
        "अभी नहीं",
    ],
)
async def test_no(said: str):
    result = await fit(said)
    assert isinstance(result, Fit) and result.value is False, said


@pytest.mark.parametrize(
    "said",
    ["hmm", "achha", "accha", "ok", "okay", "right", "अच्छा", "kya bola aapne"],
)
async def test_backchannels_and_noise_are_not_consent(said: str):
    assert isinstance(await fit(said), NoFit), said


async def test_mat_karo_is_not_a_yes():
    """'karo' alone is consent; inside 'mat karo' it must not be claimed."""
    result = await fit("mat karo")
    assert isinstance(result, Fit) and result.value is False


async def test_both_sides_is_ambiguous():
    result = await fit("haan nahi ruko")
    assert isinstance(result, Ambiguous)
    assert set(result.candidates) == {"yes", "no"}


async def test_empty_is_nofit():
    assert isinstance(await fit(""), NoFit)
