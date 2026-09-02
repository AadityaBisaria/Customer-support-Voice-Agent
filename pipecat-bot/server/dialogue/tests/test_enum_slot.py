"""EnumSlot: matching a spoken answer against the options that were offered."""

from datetime import datetime, timedelta, timezone

import pytest

from dialogue import (
    Ambiguous,
    Confidence,
    EnumSlot,
    Fit,
    FitContext,
    NoFit,
    NoFitReason,
    Utterance,
)

IST = timezone(timedelta(hours=5, minutes=30))

# The three appointment times from the worked example, with the surface forms the
# offer-building code would derive: day name, time, ordinal.
OFFER = {
    "slot_a": ["tuesday", "3pm", "the first", "first one"],
    "slot_b": ["wednesday", "10am", "the second", "second one"],
    "slot_c": ["thursday", "2pm", "the last", "third one"],
}


@pytest.fixture
def ctx() -> FitContext:
    return FitContext(now=datetime(2026, 8, 30, 12, 0, tzinfo=IST))


@pytest.fixture
def slot() -> EnumSlot:
    return EnumSlot(
        name="chosen_slot",
        prompt="Tuesday at 3, Wednesday at 10, or Thursday at 2?",
        surface=OFFER,
    )


class TestCleanHits:
    @pytest.mark.parametrize(
        ("said", "expected"),
        [
            ("let's do wednesday", "slot_b"),
            ("wednesday", "slot_b"),
            ("THURSDAY!!", "slot_c"),
            ("the first", "slot_a"),
            ("give me the first one", "slot_a"),
            ("i'll take the second one please", "slot_b"),
            ("10am works", "slot_b"),
            ("2pm", "slot_c"),
            ("um, tuesday I guess", "slot_a"),
        ],
    )
    async def test_resolves(self, slot, ctx, said, expected):
        result = await slot.fit_utterance(Utterance.from_text(said), ctx)
        assert isinstance(result, Fit)
        assert result.value == expected
        assert result.confidence is Confidence.HIGH


class TestNoFit:
    @pytest.mark.parametrize(
        "said",
        [
            "actually make it a follow-up not a new patient visit",  # a correction
            "when's my current appointment?",  # a digression
            "do I need to fast for this?",  # a knowledge question
            "friday",  # a day, but not one on offer
        ],
    )
    async def test_returns_no_surface_hit(self, slot, ctx, said):
        result = await slot.fit_utterance(Utterance.from_text(said), ctx)
        assert isinstance(result, NoFit)
        assert result.reason is NoFitReason.NO_SURFACE_HIT

    async def test_no_surface_hit_falls_through(self, slot, ctx):
        """The correction signature needs this to reach the digression check."""
        result = await slot.fit_utterance(Utterance.from_text("make it a follow-up instead"), ctx)
        assert isinstance(result, NoFit)
        assert result.reason.falls_through is True


class TestAmbiguity:
    """Two options matched: clarify, never guess the winner."""

    @pytest.fixture
    def colliding(self) -> EnumSlot:
        """A real offer can collide: two Tuesday slots at different times."""
        return EnumSlot(
            name="chosen_slot",
            prompt="Tuesday at 3, Tuesday at 5, or Thursday at 2?",
            surface={
                "slot_a": ["tuesday", "3pm", "the first"],
                "slot_b": ["tuesday", "5pm", "the second"],
                "slot_c": ["thursday", "2pm", "the last"],
            },
        )

    async def test_collision_is_ambiguous(self, colliding, ctx):
        result = await colliding.fit_utterance(Utterance.from_text("tuesday"), ctx)
        assert isinstance(result, Ambiguous)
        assert result.candidates == ("slot_a", "slot_b")

    async def test_candidates_exclude_the_non_match(self, colliding, ctx):
        """The re-prompt narrows to the contenders; slot_c is not re-offered."""
        result = await colliding.fit_utterance(Utterance.from_text("tuesday please"), ctx)
        assert isinstance(result, Ambiguous)
        assert "slot_c" not in result.candidates

    async def test_disambiguating_reply_resolves(self, colliding, ctx):
        """The second turn of the clarification exchange."""
        result = await colliding.fit_utterance(Utterance.from_text("5pm"), ctx)
        assert isinstance(result, Fit)
        assert result.value == "slot_b"


class TestLongestFormWins:
    """A specific form claims its words before a shorter one can see them."""

    @pytest.fixture
    def overlapping(self) -> EnumSlot:
        return EnumSlot(
            name="pick",
            prompt="Which?",
            surface={"a": ["the first one"], "b": ["one"]},
        )

    async def test_longer_phrase_suppresses_the_shorter(self, overlapping, ctx):
        """Without overlap suppression this would be a fabricated Ambiguous."""
        result = await overlapping.fit_utterance(Utterance.from_text("the first one"), ctx)
        assert isinstance(result, Fit)
        assert result.value == "a"

    async def test_shorter_phrase_still_matches_alone(self, overlapping, ctx):
        result = await overlapping.fit_utterance(Utterance.from_text("just one"), ctx)
        assert isinstance(result, Fit)
        assert result.value == "b"


class TestEvidence:
    async def test_reports_which_words_matched(self, slot, ctx):
        said = "let's do wednesday"
        result = await slot.fit_utterance(Utterance.from_text(said), ctx)
        assert isinstance(result, Fit)
        assert result.evidence.matcher == "EnumSlot.surface"
        assert result.evidence.matched_terms == ("wednesday",)
        start, end = result.evidence.spans[0]
        assert said[start:end] == "wednesday"

    async def test_spans_index_the_raw_text_not_the_folded(self, slot, ctx):
        """Casing and punctuation survive, because folding is length-preserving."""
        said = "THURSDAY!!"
        result = await slot.fit_utterance(Utterance.from_text(said), ctx)
        assert isinstance(result, Fit)
        start, end = result.evidence.spans[0]
        assert said[start:end] == "THURSDAY"

    async def test_latency_is_stamped(self, slot, ctx):
        result = await slot.fit_utterance(Utterance.from_text("wednesday"), ctx)
        assert result.evidence.elapsed_ms > 0

    async def test_ambiguous_carries_every_matched_span(self, ctx):
        colliding = EnumSlot(name="p", prompt="?", surface={"a": ["tuesday"], "b": ["tuesday"]})
        result = await colliding.fit_utterance(Utterance.from_text("tuesday"), ctx)
        assert isinstance(result, Ambiguous)
        assert len(result.evidence.spans) == 2


class TestNormalizationInvariant:
    """Surface forms are normalized through the same function as the transcript."""

    @pytest.mark.parametrize("said", ["follow up", "follow-up", "a FOLLOW-UP"])
    async def test_natural_spelling_in_the_surface_map_still_matches(self, ctx, said):
        slot = EnumSlot(
            name="reason",
            prompt="Why?",
            surface={"follow_up": ["follow-up", "a follow up"], "new": ["new patient"]},
        )
        result = await slot.fit_utterance(Utterance.from_text(said), ctx)
        assert isinstance(result, Fit)
        assert result.value == "follow_up"

    async def test_apostrophes_survive_on_both_sides(self, ctx):
        slot = EnumSlot(name="x", prompt="?", surface={"v": ["let's go"]})
        result = await slot.fit_utterance(Utterance.from_text("lets go"), ctx)
        assert isinstance(result, Fit)


class TestSpeculation:
    async def test_interim_hit_is_demoted(self, slot, ctx):
        result = await slot.fit_utterance(
            Utterance.from_text("let's do wednesday", is_final=False), ctx
        )
        assert isinstance(result, Fit)
        assert result.speculative is True
        assert result.confidence is Confidence.MEDIUM


class TestConstruction:
    def test_values_follow_offer_order(self, slot):
        assert slot.values == ("slot_a", "slot_b", "slot_c")

    async def test_blank_surface_forms_are_dropped(self, ctx):
        slot = EnumSlot(name="x", prompt="?", surface={"v": ["", "  ", "okay"]})
        assert isinstance(await slot.fit_utterance(Utterance.from_text("okay"), ctx), Fit)
        assert isinstance(await slot.fit_utterance(Utterance.from_text("nothing"), ctx), NoFit)
