"""The consent classifier: deterministic Hinglish yes/no for the confirm gate.

Backchannels are not consent. "hmm", "achha", bare "ok", "अच्छा" appear in
neither surface set, so they produce NoFit and the gate re-asks. Both sides
firing ("haan... nahi ruko") is a genuine Ambiguous, never a guess. The
phrase matching itself is EnumSlot's, so yes and no forms are pooled
longest-first — "mat karo" can never be read as a bare "karo" yes.

Devanagari does not fold to Latin, so every Hindi form is listed in both
scripts explicitly.
"""

from dataclasses import dataclass

from dialogue.enum_slot import EnumSlot
from dialogue.results import Ambiguous, Fit, FitResult
from dialogue.slots import FitContext, Slot
from dialogue.utterance import Utterance

_YES_FORMS = [
    # romanized
    "haan",
    "han",
    "haa",
    "ji",
    "ji haan",
    "haanji",
    "hanji",
    "bilkul",
    "zaroor",
    "theek hai",
    "thik hai",
    "kar do",
    "kar dijiye",
    "kardo",
    "karo",
    "confirm",
    "confirm karo",
    "sahi hai",
    "ok karo",
    "go ahead",
    "yes",
    "yeah",
    "yep",
    "sure",
    "correct",
    "done karo",
    # Devanagari
    "हां",
    "हाँ",
    "हा",
    "जी",
    "जी हां",
    "जी हाँ",
    "हांजी",
    "बिल्कुल",
    "ज़रूर",
    "जरूर",
    "ठीक है",
    "कर दो",
    "कर दीजिए",
    "करो",
    "सही है",
    # Punjabi / Gurmukhi
    "ਹਾਂਜੀ",
    "ਹਾਂ",
]

_NO_FORMS = [
    # romanized
    "nahi",
    "nahin",
    "nahi karna",
    "mat",
    "mat karo",
    "rehne do",
    "rahne do",
    "cancel karo",
    "cancel kar do",
    "ruko",
    "ruk jao",
    "rok do",
    "wait karo",
    "no",
    "nope",
    "nah",
    "dont",
    "do not",
    "abhi nahi",
    # Devanagari
    "नहीं",
    "नही",
    "मत",
    "मत करो",
    "रहने दो",
    "रुको",
    "रुक जाओ",
    "रोक दो",
    "अभी नहीं",
]

_CLASSIFIER = EnumSlot(
    name="yes_no",
    prompt="haan ya nahi?",
    surface={"yes": _YES_FORMS, "no": _NO_FORMS},
)


@dataclass(frozen=True, slots=True, kw_only=True)
class YesNoSlot(Slot[bool]):
    """Consent as a boolean. Delegates matching to a pooled EnumSlot."""

    async def fit(self, u: Utterance, ctx: FitContext) -> FitResult:
        result = await _CLASSIFIER.fit(u, ctx)
        if isinstance(result, Fit):
            return Fit(
                slot=self.name,
                value=result.value == "yes",
                confidence=result.confidence,
                evidence=result.evidence,
            )
        if isinstance(result, Ambiguous):
            return Ambiguous(slot=self.name, candidates=result.candidates, evidence=result.evidence)
        return result  # NoFit passes through: backchannel or unrelated speech
