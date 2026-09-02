"""The result algebra a slot-fit attempt returns.

Four variants, because each maps to exactly one distinct action in the resolution
cascade. Collapsing any two of them loses information the cascade needs:

    Fit (HIGH/MEDIUM)  commit the value, advance the node, short-circuit
    Fit (LOW)          hold; run the digression check anyway; commit only if
                       nothing outbids it. This is what stops a FreeText slot
                       from swallowing "when's my current appointment?"
    Ambiguous          clarify, listing ONLY the candidates
    NoFit              fall through to the digression check
    Rejected           re-prompt with the constraint; do NOT fall through

``NoFit`` is load-bearing: it is the trigger to fall through, and the combination
of ``NoFit`` plus "the digression check matched the already-active flow" is the
signature of a *correction* ("actually make it a follow-up").
"""

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any


class Confidence(StrEnum):
    """How much the matcher trusts its own answer.

    Deliberately an enum and not a float. A token-overlap score from an enum
    matcher and a date parser's certainty are not measured in the same units, so
    a shared float scale would invite comparisons that mean nothing. Each matcher
    decides its own bands; the cascade only ever asks "is this LOW?".

    Note this is MATCH confidence, never ASR confidence -- Pipecat surfaces no
    transcription confidence on its frames (it is buried in the provider-specific
    ``TranscriptionFrame.result``), so the two must not be conflated.

    Ordering is defined explicitly below. It has to be: ``StrEnum`` members compare
    as strings, so out of the box ``HIGH > MEDIUM`` is False -- "high" sorts before
    "medium" alphabetically. Left alone, that silently inverts every threshold a
    caller writes.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        """LOW < MEDIUM < HIGH, as an int. The basis of every comparison below."""
        return _RANK[self]

    def __lt__(self, other: object) -> bool:
        if isinstance(other, Confidence):
            return self.rank < other.rank
        return NotImplemented

    def __le__(self, other: object) -> bool:
        if isinstance(other, Confidence):
            return self.rank <= other.rank
        return NotImplemented

    def __gt__(self, other: object) -> bool:
        if isinstance(other, Confidence):
            return self.rank > other.rank
        return NotImplemented

    def __ge__(self, other: object) -> bool:
        if isinstance(other, Confidence):
            return self.rank >= other.rank
        return NotImplemented


_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}


class NoFitReason(StrEnum):
    """Why a matcher declined -- and, via ``falls_through``, what to do about it.

    "NoFit means run the digression check" is *nearly* true, and the exceptions
    matter. Silence has no text to embed; an explicit refusal is a instruction to
    the flow, not a request to switch flows. Sending either to a cosine similarity
    against flow vectors would score noise and could switch flows on nothing.

    Every production voice stack draws this line. Dialogflow CX separates
    ``sys.no-match-*`` from ``sys.no-input-*``; Rasa has a dedicated
    ``pattern_user_silence`` distinct from its digression handling, and a
    ``pattern_skip_question`` for refusals.
    """

    NO_SURFACE_HIT = "no_surface_hit"
    """Nothing in the utterance resembled this slot. THE digression trigger --
    paired with "the digression check matched the active flow", this is the
    correction signature."""

    UNPARSEABLE = "unparseable"
    """Shaped like an answer but not readable ("the umpteenth of Febtober")."""

    EMPTY = "empty"
    """Text arrived with no alphanumeric content. Nothing to embed."""

    NO_INPUT = "no_input"
    """The caller said nothing -- silence or timeout. Not a digression: there is
    no utterance to route. Escalate the re-prompt instead."""

    DECLINED = "declined"
    """The caller explicitly refused the slot ("skip that", "none of those").
    An instruction to this flow, not a request to leave it."""

    @property
    def falls_through(self) -> bool:
        """Whether the cascade should continue to the digression check."""
        return self in _FALLS_THROUGH


_FALLS_THROUGH = frozenset({NoFitReason.NO_SURFACE_HIT, NoFitReason.UNPARSEABLE})


@dataclass(frozen=True, slots=True)
class Evidence:
    """Why a matcher decided what it decided.

    Structural rather than prose, so a downstream compliance check can answer
    "which words of the caller's speech justify this value" without re-running
    or re-parsing anything. That question is the whole point of a guardrail layer,
    and it cannot be reconstructed after the fact.

    Carries ``elapsed_ms`` too, stamped centrally by ``Slot.fit_utterance``, which
    gives per-matcher latency measurement for the cascade's budget for free.
    """

    matcher: str = "unspecified"
    """Stable identifier, e.g. "EnumSlot.surface". Not a class name -- a slot type
    with several strategies should say which one fired."""

    spans: tuple[tuple[int, int], ...] = ()
    """Char extents in ``Utterance.raw`` that justified the decision. Valid only
    because ``fold_for_matching`` preserves length 1:1."""

    matched_terms: tuple[str, ...] = ()
    """The surface forms that hit, e.g. ("wednesday",)."""

    elapsed_ms: float = 0.0
    notes: str = ""


@dataclass(frozen=True, slots=True)
class Fit[T]:
    """The utterance supplied a value for the slot."""

    slot: str
    value: T
    confidence: Confidence
    speculative: bool = False
    """Derived from an interim transcript. Never commit one -- it exists so
    downstream work can start early, and is discarded if the final differs."""

    evidence: Evidence = field(default_factory=Evidence)


@dataclass(frozen=True, slots=True)
class Ambiguous:
    """The utterance matched more than one candidate, and guessing is not allowed.

    Carries the candidate set rather than merely failing, so the re-prompt can
    narrow to just those ("Tuesday at 3, or Wednesday at 10?") instead of
    re-reading the full list. Two turns beats one wrong booking.
    """

    slot: str
    candidates: tuple[str, ...]
    evidence: Evidence = field(default_factory=Evidence)


@dataclass(frozen=True, slots=True)
class NoFit:
    """The utterance supplied nothing for this slot. Fall through to the next layer."""

    reason: NoFitReason = NoFitReason.NO_SURFACE_HIT
    evidence: Evidence = field(default_factory=Evidence)


@dataclass(frozen=True, slots=True)
class Rejected[T]:
    """A value parsed cleanly but violated a constraint.

    Distinct from NoFit and that distinction is the whole point: "can I book next
    February?" parses to a perfectly good date and is refused by the 14-day
    horizon. Reporting NoFit would send it to the digression check, which would
    look for another flow to switch to. It should re-ask with the reason instead.
    """

    slot: str
    value: T
    constraint: str
    """Human-readable reason, used to build the re-prompt."""

    evidence: Evidence = field(default_factory=Evidence)


type FitResult = Fit[Any] | Ambiguous | NoFit | Rejected[Any]


def stamp_elapsed(result: FitResult, elapsed_ms: float) -> FitResult:
    """Record how long the matcher took, on whichever variant came back.

    Done centrally so every matcher is measured the same way and none has to
    remember to do it.
    """
    return replace(result, evidence=replace(result.evidence, elapsed_ms=elapsed_ms))


def apply_speculation(result: FitResult, is_final: bool) -> FitResult:
    """Enforce the speculation policy in one place, rather than trusting matchers.

    A fit derived from an interim transcript is marked speculative and capped at
    MEDIUM, so it can never short-circuit the cascade the way a settled HIGH fit
    does. Applied by the caller after ``fit()`` returns, so an individual matcher
    cannot forget to do it.
    """
    if is_final or not isinstance(result, Fit):
        return result
    return replace(
        result,
        confidence=min(result.confidence, Confidence.MEDIUM),
        speculative=True,
    )
