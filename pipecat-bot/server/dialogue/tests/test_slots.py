"""The Slot ABC, proved by a concrete subclass defined here in the test.

Defining the subclass locally keeps this file honest: it exercises the base
contract without depending on any real matcher landing first.
"""

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from dialogue import (
    Ambiguous,
    Confidence,
    ConstantSlot,
    Fit,
    FitContext,
    FitResult,
    NoFit,
    NoFitReason,
    Slot,
    Utterance,
)

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture
def ctx() -> FitContext:
    return FitContext(now=datetime(2026, 8, 30, 12, 0, tzinfo=IST))


@dataclass(frozen=True, slots=True, kw_only=True)
class YesNoSlot(Slot[bool]):
    """A concrete slot covering every branch of the result algebra."""

    allow_no: bool = True

    async def fit(self, u: Utterance, ctx: FitContext) -> FitResult:
        yes = {"yes", "yeah", "yep", "sure"} & u.token_set
        no = {"no", "nope", "nah"} & u.token_set
        if yes and no:
            return Ambiguous(slot=self.name, candidates=("yes", "no"))
        if no and not self.allow_no:
            from dialogue import Rejected

            return Rejected(slot=self.name, value=False, constraint="I do need a yes to continue")
        if yes or no:
            return Fit(slot=self.name, value=bool(yes), confidence=Confidence.HIGH)
        return NoFit(reason=NoFitReason.NO_SURFACE_HIT)


class TestABC:
    def test_base_cannot_be_instantiated(self):
        with pytest.raises(TypeError, match="abstract"):
            Slot(name="x", prompt="y")  # type: ignore[abstract]

    def test_subclass_is_frozen(self):
        slot = YesNoSlot(name="confirmed", prompt="Correct?")
        with pytest.raises(dataclasses.FrozenInstanceError):
            slot.name = "other"  # type: ignore[misc]

    def test_kw_only_lets_subclasses_add_fields_after_defaulted_base_fields(self):
        """``required`` has a default on the base; ``allow_no`` still works."""
        slot = YesNoSlot(name="c", prompt="p", allow_no=False)
        assert slot.required is True
        assert slot.allow_no is False


class TestYesNoSlot:
    async def test_fit_yes(self, ctx):
        slot = YesNoSlot(name="confirmed", prompt="Correct?")
        result = await slot.fit_utterance(Utterance.from_text("yeah that's right"), ctx)
        assert isinstance(result, Fit)
        assert result.value is True
        assert result.confidence is Confidence.HIGH

    async def test_fit_no(self, ctx):
        slot = YesNoSlot(name="confirmed", prompt="Correct?")
        result = await slot.fit_utterance(Utterance.from_text("nope"), ctx)
        assert isinstance(result, Fit)
        assert result.value is False

    async def test_no_signal_falls_through(self, ctx):
        """The load-bearing case: NoFit is what sends the turn to layer 3."""
        slot = YesNoSlot(name="confirmed", prompt="Correct?")
        result = await slot.fit_utterance(
            Utterance.from_text("when's my current appointment?"), ctx
        )
        assert isinstance(result, NoFit)
        assert result.reason is NoFitReason.NO_SURFACE_HIT

    async def test_ambiguous_keeps_both_candidates(self, ctx):
        slot = YesNoSlot(name="confirmed", prompt="Correct?")
        result = await slot.fit_utterance(Utterance.from_text("yes no maybe"), ctx)
        assert isinstance(result, Ambiguous)
        assert result.candidates == ("yes", "no")

    async def test_rejected_carries_the_constraint(self, ctx):
        from dialogue import Rejected

        slot = YesNoSlot(name="confirmed", prompt="Correct?", allow_no=False)
        result = await slot.fit_utterance(Utterance.from_text("no"), ctx)
        assert isinstance(result, Rejected)
        assert "yes" in result.constraint


class TestSpeculationPolicy:
    """Enforced once in ``fit_utterance`` so no matcher can forget it."""

    async def test_interim_fit_is_demoted_and_marked(self, ctx):
        slot = YesNoSlot(name="confirmed", prompt="Correct?")
        result = await slot.fit_utterance(Utterance.from_text("yeah", is_final=False), ctx)
        assert isinstance(result, Fit)
        assert result.speculative is True
        assert result.confidence is Confidence.MEDIUM

    async def test_final_fit_keeps_high(self, ctx):
        slot = YesNoSlot(name="confirmed", prompt="Correct?")
        result = await slot.fit_utterance(Utterance.from_text("yeah"), ctx)
        assert isinstance(result, Fit)
        assert result.speculative is False
        assert result.confidence is Confidence.HIGH

    async def test_no_interim_fit_can_reach_high(self, ctx):
        """The whole point: an interim can never short-circuit as a settled HIGH."""
        slot = YesNoSlot(name="confirmed", prompt="Correct?")
        result = await slot.fit_utterance(Utterance.from_text("yes", is_final=False), ctx)
        assert isinstance(result, Fit)
        assert result.confidence is not Confidence.HIGH

    async def test_raw_fit_is_undemoted(self, ctx):
        """``fit`` itself is unpolicied; ``fit_utterance`` is what applies the cap."""
        slot = YesNoSlot(name="confirmed", prompt="Correct?")
        raw = await slot.fit(Utterance.from_text("yeah", is_final=False), ctx)
        assert isinstance(raw, Fit)
        assert raw.confidence is Confidence.HIGH
        assert raw.speculative is False


class TestFitContext:
    def test_rejects_naive_now(self):
        """A naive ``now`` would make "next Wednesday" a day out near midnight."""
        with pytest.raises(ValueError, match="timezone-aware"):
            FitContext(now=datetime(2026, 8, 30, 12, 0))

    def test_tz_derives_from_now(self, ctx):
        assert ctx.tz is IST

    def test_is_frozen(self, ctx):
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.locale = "fr-FR"

    def test_filled_defaults_empty(self, ctx):
        assert ctx.filled == {}


class TestConstantSlot:
    async def test_fits_when_there_is_content(self, ctx):
        slot = ConstantSlot(name="reason", prompt="Why?", value="follow_up")
        result = await slot.fit_utterance(Utterance.from_text("a follow up"), ctx)
        assert isinstance(result, Fit)
        assert result.value == "follow_up"

    async def test_empty_utterance_is_not_a_fit(self, ctx):
        slot = ConstantSlot(name="reason", prompt="Why?", value="follow_up")
        result = await slot.fit_utterance(Utterance.from_text("..."), ctx)
        assert isinstance(result, NoFit)
        assert result.reason is NoFitReason.EMPTY


class TestAttempts:
    """Attempt count is turn state on the context, never on the frozen Slot."""

    def test_defaults_to_zero(self, ctx):
        assert ctx.attempts == 0

    def test_is_carried_on_the_context(self):
        ctx = FitContext(now=datetime(2026, 8, 30, 12, 0, tzinfo=IST), attempts=2)
        assert ctx.attempts == 2

    def test_slot_has_no_attempt_state(self):
        """A Slot is a shared spec -- two live frames may point at the same one."""
        slot = YesNoSlot(name="confirmed", prompt="Correct?")
        assert not hasattr(slot, "attempts")
