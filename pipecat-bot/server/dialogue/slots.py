"""The base slot abstraction: a question the bot is waiting on an answer to.

A slot is an immutable *specification* -- what to ask, and how to recognise an
answer. It holds no per-conversation state. Attempt counts, filled values and the
pending-slot pointer live in the dialogue frame, so a single slot definition can
be shared across every concurrent session.
"""

import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from enum import StrEnum
from typing import Any

from dialogue.results import (
    Confidence,
    Evidence,
    Fit,
    FitResult,
    NoFit,
    NoFitReason,
    apply_speculation,
    stamp_elapsed,
)
from dialogue.utterance import Utterance


class InterruptPolicy(StrEnum):
    """Where this slot sits relative to the digression check.

    Cascade order is a property of the slot being collected, not a global
    constant, because the right order genuinely differs by slot. Rasa reached the
    same conclusion and put ``force_slot_filling`` on the collect step in 3.12.
    """

    ALLOW = "allow"
    """Default. Slot-fit first (~1ms); fall through to the digression check only
    on a NoFit whose reason falls through."""

    DIGRESSION_FIRST = "digression_first"
    """Run the digression check BEFORE attempting the fit. For greedy slots that
    would otherwise swallow everything: at a free-text slot, "when's my current
    appointment?" is a perfectly good free-text value, so the only way to notice
    it is a digression is to look before asking."""

    SUPPRESS = "suppress"
    """Never leave this slot for a digression. For a value that must be collected
    atomically -- an account number, a one-time passcode -- where wandering off
    mid-collection means starting over and, for PII, re-reading it aloud."""


@dataclass(frozen=True, slots=True, kw_only=True)
class FitContext:
    """Everything a matcher needs that is not in the utterance itself."""

    now: datetime
    """Reference instant for relative dates, and the source of the caller's
    timezone. Injected rather than read from the clock inside a matcher, which is
    what makes "next week" deterministically testable.

    Must be timezone-aware. A naive value is rejected rather than quietly assumed
    to be UTC: "next Wednesday" resolves differently either side of midnight, so
    guessing the zone would produce a booking that is silently a day out.
    """

    locale: str = "en-US"

    filled: Mapping[str, Any] = field(default_factory=dict)
    """Slots already filled on this frame, for constraints that depend on an
    earlier answer (an end date that must follow a start date)."""

    attempts: int = 0
    """How many times this slot has already been asked and missed.

    Lives here rather than on the Slot because a Slot is a frozen spec shared
    across sessions. A matcher may legitimately widen its tolerance on a retry,
    and the flow escalates the prompt and eventually gives up -- Dialogflow CX
    numbers its re-prompt handlers ``sys.no-match-1`` through ``sys.no-match-6``,
    Lex defaults to three retries before failing the elicitation."""

    def __post_init__(self) -> None:
        if self.now.tzinfo is None or self.now.tzinfo.utcoffset(self.now) is None:
            raise ValueError("FitContext.now must be timezone-aware")

    @property
    def tz(self) -> tzinfo:
        """The caller's timezone, derived from ``now`` so the two cannot disagree."""
        assert self.now.tzinfo is not None  # guaranteed by __post_init__
        return self.now.tzinfo


@dataclass(frozen=True, slots=True, kw_only=True)
class Slot[T](ABC):
    """A value the dialogue needs, and the rule for recognising it in speech.

    Generic in the value type, so ``DateSlot(Slot[date])`` yields ``Fit[date]``
    and a caller reading ``.value`` gets a real type rather than ``Any``.

    Subclasses are frozen dataclasses. ``kw_only`` throughout, so a subclass may
    add fields without defaults even though the base has some.
    """

    name: str
    prompt: str
    """The question to ask, and to re-ask on return from a digression -- never
    assume the user remembers where they were."""

    required: bool = True

    interrupt: InterruptPolicy = InterruptPolicy.ALLOW
    """Where this slot sits relative to the digression check. Declarative -- the
    cascade reads it; the matcher never does."""

    @abstractmethod
    async def fit(self, u: Utterance, ctx: FitContext) -> FitResult:
        """Try to read a value for this slot out of the utterance.

        Async so that a slot type can await I/O -- an embedding lookup, an account
        validation, a compliance check -- without forcing a sync-to-async refactor
        through every caller later. Most implementations never await anything.

        Implementations should not worry about speculation; ``fit_utterance``
        applies that policy uniformly.
        """

    async def fit_utterance(self, u: Utterance, ctx: FitContext) -> FitResult:
        """Call ``fit`` and apply the speculation policy. Prefer this at call sites.

        Keeping the interim-transcript rule here means a matcher cannot forget it:
        a fit read off a partial transcript comes back marked speculative and
        capped at MEDIUM, so it can never short-circuit the cascade. Latency is
        stamped here for the same reason -- uniformly, or the budget is a guess.
        """
        started = time.perf_counter()
        result = await self.fit(u, ctx)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return apply_speculation(stamp_elapsed(result, elapsed_ms), u.is_final)


@dataclass(frozen=True, slots=True, kw_only=True)
class ConstantSlot[T](Slot[T]):
    """A slot that answers with a fixed value whenever the utterance has content.

    Exists to prove the base interface end to end without dragging in the real
    matchers. Genuinely useful as a stub while a flow is being sketched.
    """

    value: T
    confidence: Confidence = Confidence.HIGH

    async def fit(self, u: Utterance, ctx: FitContext) -> FitResult:
        if not u.tokens:
            return NoFit(reason=NoFitReason.EMPTY, evidence=Evidence(matcher="ConstantSlot.empty"))
        return Fit(
            slot=self.name,
            value=self.value,
            confidence=self.confidence,
            evidence=Evidence(matcher="ConstantSlot.constant"),
        )
