"""Spoken phone-number capture: ten digits from mixed-form Hinglish speech.

Handles ASCII digit runs ("98765 43210"), Devanagari numerals (०-९), English
digit words with double/triple lookahead, and Hindi digit words in both
scripts. Words like "do" (=2) collide with English, but this slot only runs
while the phone gate is active — the caller was just asked for their number.

Returns the digits as a plain string; the store's PhoneNumber value object
owns final validity (this package must not import the store).
"""

from dataclasses import dataclass

from dialogue.results import Confidence, Evidence, Fit, FitResult, NoFit, NoFitReason
from dialogue.slots import FitContext, Slot
from dialogue.utterance import Utterance, normalize

_RAW_WORD_DIGITS = {
    # English
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    # Romanized Hindi
    "shunya": "0",
    "ek": "1",
    "do": "2",
    "teen": "3",
    "tin": "3",
    "char": "4",
    "chaar": "4",
    "paanch": "5",
    "panch": "5",
    "pach": "5",
    "chhe": "6",
    "che": "6",
    "cheh": "6",
    "chah": "6",
    "saat": "7",
    "sat": "7",
    "aath": "8",
    "ath": "8",
    "nau": "9",
    "no": "9",
    # Devanagari words
    "शून्य": "0",
    "एक": "1",
    "दो": "2",
    "तीन": "3",
    "चार": "4",
    "पांच": "5",
    "पाँच": "5",
    "छह": "6",
    "छे": "6",
    "सात": "7",
    "आठ": "8",
    "नौ": "9",
}

# One normalizer, both sides (the dialogue-layer invariant): transcripts are
# folded by `normalize`, which also strips Devanagari combining marks — so the
# lookup keys must go through the same fold or "शून्य" would never match.
_WORD_DIGITS: dict[str, str] = {}
for _word, _digit in _RAW_WORD_DIGITS.items():
    for _token in normalize(_word):
        _WORD_DIGITS[_token] = _digit

_REPEATERS = {"double": 2, "triple": 3}


def extract_digits(tokens: tuple[str, ...]) -> str:
    """Digits in spoken order, from digit runs, numerals, and digit words."""
    out: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in _REPEATERS and i + 1 < len(tokens):
            digit = _single_digit(tokens[i + 1])
            if digit is not None:
                out.append(digit * _REPEATERS[token])
                i += 2
                continue
        run = _digit_run(token)
        if run is not None:
            out.append(run)
        i += 1
    return "".join(out)


def _digit_run(token: str) -> str | None:
    if token.isdigit():
        # int() normalizes any Unicode decimal digit (incl. Devanagari ०-९).
        return "".join(str(int(ch)) for ch in token)
    return _WORD_DIGITS.get(token)


def _single_digit(token: str) -> str | None:
    run = _digit_run(token)
    return run if run is not None and len(run) == 1 else None


def strip_country_prefix(digits: str) -> str:
    """'+91' or a leading trunk '0' before a full mobile number."""
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        return digits[1:]
    return digits


@dataclass(frozen=True, slots=True, kw_only=True)
class PhoneSlot(Slot[str]):
    """Fits exactly ten digits; partial captures re-ask rather than guess."""

    async def fit(self, u: Utterance, ctx: FitContext) -> FitResult:
        if not u.tokens:
            return NoFit(reason=NoFitReason.EMPTY, evidence=Evidence(matcher="PhoneSlot"))

        digits = strip_country_prefix(extract_digits(u.tokens))
        if len(digits) == 10:
            return Fit(
                slot=self.name,
                value=digits,
                confidence=Confidence.HIGH,
                evidence=Evidence(matcher="PhoneSlot", notes=f"{len(digits)} digits"),
            )
        if digits:
            return NoFit(
                reason=NoFitReason.UNPARSEABLE,
                evidence=Evidence(
                    matcher="PhoneSlot", notes=f"heard {len(digits)} digits, need 10"
                ),
            )
        return NoFit(reason=NoFitReason.NO_SURFACE_HIT, evidence=Evidence(matcher="PhoneSlot"))
