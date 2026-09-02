"""Slot-filling primitives for the dialogue cascade.

Layer 2 of the resolution cascade: given the slot the bot is currently waiting on,
decide whether an utterance answers it -- without embeddings and without an LLM.
The result also drives the layers below, since ``NoFit`` is what tells the cascade
to fall through to the digression check.
"""

from dialogue.enum_slot import EnumSlot
from dialogue.results import (
    Ambiguous,
    Confidence,
    Evidence,
    Fit,
    FitResult,
    NoFit,
    NoFitReason,
    Rejected,
    apply_speculation,
    stamp_elapsed,
)
from dialogue.slots import ConstantSlot, FitContext, InterruptPolicy, Slot
from dialogue.utterance import Utterance, normalize

__all__ = [
    "Ambiguous",
    "Confidence",
    "ConstantSlot",
    "EnumSlot",
    "Evidence",
    "Fit",
    "FitContext",
    "FitResult",
    "InterruptPolicy",
    "NoFit",
    "NoFitReason",
    "Rejected",
    "Slot",
    "Utterance",
    "apply_speculation",
    "normalize",
    "stamp_elapsed",
]
