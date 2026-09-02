"""PhoneSlot: spoken-digit extraction across scripts and word forms."""

from datetime import UTC, datetime, timezone

import pytest

from dialogue.phone import PhoneSlot, extract_digits, strip_country_prefix
from dialogue.results import Fit, NoFit, NoFitReason
from dialogue.slots import FitContext
from dialogue.utterance import Utterance, normalize

SLOT = PhoneSlot(name="phone", prompt="Apna registered mobile number boliye")
CTX = FitContext(now=datetime(2026, 9, 2, 12, 0, tzinfo=UTC))


async def fit(said: str):
    return await SLOT.fit_utterance(Utterance.from_text(said), CTX)


@pytest.mark.parametrize(
    "said",
    [
        "9876543210",
        "98765 43210",
        "9 8 7 6 5 4 3 2 1 0",
        "nine eight seven six five four three two one zero",
        "nau aath saat chhe paanch char teen do ek shunya",
        "नौ आठ सात छह पांच चार तीन दो एक शून्य",
        "९८७६५४३२१०",
        "+91 98765 43210",
        "0 98765 43210",
        "mera number hai 98765 43210",
    ],
)
async def test_ten_digit_captures(said: str):
    result = await fit(said)
    assert isinstance(result, Fit), said
    assert result.value == "9876543210", said


async def test_double_and_triple():
    # nine eight [double seven -> 77] [triple two -> 222] zero one four
    result = await fit("nine eight double seven triple two zero one four")
    assert isinstance(result, Fit)
    assert result.value == "9877222014"


async def test_partial_reasks():
    result = await fit("98765")
    assert isinstance(result, NoFit)
    assert result.reason is NoFitReason.UNPARSEABLE


async def test_no_digits_falls_through():
    result = await fit("mujhe order return karna hai")
    assert isinstance(result, NoFit)
    assert result.reason is NoFitReason.NO_SURFACE_HIT


async def test_empty():
    result = await fit("")
    assert isinstance(result, NoFit)


def test_extract_digits_mixes_forms():
    tokens = normalize("nine 8 saat ६ five")
    assert extract_digits(tokens) == "98765"


def test_strip_country_prefix():
    assert strip_country_prefix("919876543210") == "9876543210"
    assert strip_country_prefix("09876543210") == "9876543210"
    assert strip_country_prefix("9876543210") == "9876543210"
