"""The result algebra, and the cascade contract each variant encodes."""

import dataclasses

import pytest

from dialogue import (
    Ambiguous,
    Confidence,
    Fit,
    NoFit,
    NoFitReason,
    Rejected,
    apply_speculation,
)

# The reason there are four variants rather than three: each maps to exactly one
# action. Layers 3 and 4 depend on this mapping, so it is pinned here -- if a
# variant's meaning ever drifts, this table fails loudly rather than silently
# rerouting turns.
SHORT_CIRCUIT = "commit and short-circuit"
RUN_LAYER_3 = "hold, run digression check"
CLARIFY = "clarify among candidates"
FALL_THROUGH = "fall through to digression check"
REPROMPT = "re-prompt with constraint"
REPROMPT_SLOT = "re-ask the slot, escalating"


def cascade_action(result) -> str:
    """The dispatch the cascade performs. Ordering here is load-bearing."""
    match result:
        # MUST precede the general Fit case, or the FreeText guard is dead code.
        case Fit(confidence=Confidence.LOW):
            return RUN_LAYER_3
        case Fit():
            return SHORT_CIRCUIT
        case Ambiguous():
            return CLARIFY
        # Routing is on (variant, reason), not the variant alone: silence and an
        # explicit refusal are NoFits that must not reach the digression check.
        case NoFit(reason=reason) if reason.falls_through:
            return FALL_THROUGH
        case NoFit():
            return REPROMPT_SLOT
        case Rejected():
            return REPROMPT
    raise AssertionError(f"unhandled variant: {type(result).__name__}")


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (Fit(slot="s", value="v", confidence=Confidence.HIGH), SHORT_CIRCUIT),
        (Fit(slot="s", value="v", confidence=Confidence.MEDIUM), SHORT_CIRCUIT),
        (Fit(slot="s", value="v", confidence=Confidence.LOW), RUN_LAYER_3),
        (Ambiguous(slot="s", candidates=("a", "b")), CLARIFY),
        (NoFit(), FALL_THROUGH),
        (Rejected(slot="s", value="v", constraint="within 14 days"), REPROMPT),
    ],
)
def test_cascade_action_table(result, expected):
    assert cascade_action(result) == expected


def test_every_variant_is_covered():
    """Guards against adding a fifth variant without teaching the cascade about it."""
    variants = [
        Fit(slot="s", value=1, confidence=Confidence.HIGH),
        Ambiguous(slot="s", candidates=("a",)),
        NoFit(),
        Rejected(slot="s", value=1, constraint="c"),
    ]
    assert {cascade_action(v) for v in variants} == {
        SHORT_CIRCUIT,
        CLARIFY,
        FALL_THROUGH,
        REPROMPT,
    }


class TestNoFitRouting:
    """Not every NoFit is a digression. Silence and refusal are not."""

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            (NoFitReason.NO_SURFACE_HIT, True),
            (NoFitReason.UNPARSEABLE, True),
            (NoFitReason.EMPTY, False),
            (NoFitReason.NO_INPUT, False),
            (NoFitReason.DECLINED, False),
        ],
    )
    def test_falls_through_classification(self, reason, expected):
        assert reason.falls_through is expected

    def test_every_reason_is_classified(self):
        """A newly added reason fails here until someone decides its routing."""
        classified = {
            NoFitReason.NO_SURFACE_HIT,
            NoFitReason.UNPARSEABLE,
            NoFitReason.EMPTY,
            NoFitReason.NO_INPUT,
            NoFitReason.DECLINED,
        }
        assert set(NoFitReason) == classified

    def test_silence_does_not_reach_the_digression_check(self):
        """There is no text to embed; a cosine against flow vectors is noise."""
        assert cascade_action(NoFit(reason=NoFitReason.NO_INPUT)) == REPROMPT_SLOT

    def test_refusal_does_not_reach_the_digression_check(self):
        """ "Skip that" instructs the active flow; it does not request another."""
        assert cascade_action(NoFit(reason=NoFitReason.DECLINED)) == REPROMPT_SLOT

    def test_no_surface_hit_still_falls_through(self):
        """The correction signature depends on this one still routing to layer 3."""
        assert cascade_action(NoFit(reason=NoFitReason.NO_SURFACE_HIT)) == FALL_THROUGH


def test_low_confidence_pattern_must_come_first():
    """The FreeText guard only works because of the case ordering above.

    A LOW fit and a HIGH fit are both ``Fit``, so a general ``case Fit()`` placed
    first would swallow the LOW branch and a FreeText slot would once again eat
    every digression.
    """
    low = Fit(slot="notes", value="when is my appointment?", confidence=Confidence.LOW)
    assert cascade_action(low) == RUN_LAYER_3
    assert cascade_action(low) != SHORT_CIRCUIT


def test_ambiguous_carries_candidates_for_a_narrowed_reprompt():
    """Re-prompt lists only the contenders, not the whole original offer."""
    result = Ambiguous(slot="chosen_slot", candidates=("slot_a", "slot_b"))
    assert result.candidates == ("slot_a", "slot_b")
    assert "slot_c" not in result.candidates


def test_rejected_is_distinct_from_nofit():
    """A parsed-but-out-of-range value must not trigger a digression check."""
    rejected = Rejected(slot="earliest", value="2027-02-01", constraint="within 14 days")
    assert cascade_action(rejected) != cascade_action(NoFit())
    assert rejected.value == "2027-02-01"


class TestApplySpeculation:
    """Interim transcripts may be matched, but never trusted or committed."""

    def test_final_passes_through_untouched(self):
        fit = Fit(slot="s", value="v", confidence=Confidence.HIGH)
        assert apply_speculation(fit, is_final=True) is fit

    def test_interim_high_is_demoted_and_marked(self):
        fit = Fit(slot="s", value="v", confidence=Confidence.HIGH)
        out = apply_speculation(fit, is_final=False)
        assert isinstance(out, Fit)
        assert out.confidence is Confidence.MEDIUM
        assert out.speculative is True
        assert out.value == "v"

    def test_interim_low_stays_low(self):
        """Demotion caps confidence; it must not promote a LOW fit to MEDIUM."""
        fit = Fit(slot="s", value="v", confidence=Confidence.LOW)
        out = apply_speculation(fit, is_final=False)
        assert isinstance(out, Fit)
        assert out.confidence is Confidence.LOW
        assert out.speculative is True

    @pytest.mark.parametrize(
        "result",
        [
            Ambiguous(slot="s", candidates=("a",)),
            NoFit(),
            Rejected(slot="s", value=1, constraint="c"),
        ],
    )
    def test_non_fit_variants_are_untouched(self, result):
        assert apply_speculation(result, is_final=False) is result

    def test_speculative_fit_never_short_circuits_at_high(self):
        """The point of the policy: no interim result can commit as a settled HIGH."""
        out = apply_speculation(
            Fit(slot="s", value="v", confidence=Confidence.HIGH), is_final=False
        )
        assert isinstance(out, Fit)
        assert out.confidence is not Confidence.HIGH


@pytest.mark.parametrize(
    ("result", "attr"),
    [
        (Fit(slot="s", value=1, confidence=Confidence.HIGH), "slot"),
        (Ambiguous(slot="s", candidates=("a",)), "candidates"),
        (NoFit(), "reason"),
        (Rejected(slot="s", value=1, constraint="c"), "constraint"),
    ],
)
def test_variants_are_frozen(result, attr):
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(result, attr, "mutated")


def test_nofit_defaults_to_no_surface_hit():
    assert NoFit().reason is NoFitReason.NO_SURFACE_HIT


class TestConfidenceOrdering:
    """StrEnum compares as a string, which orders these wrong. Guard the fix.

    Without explicit ordering, ``Confidence.HIGH > Confidence.MEDIUM`` is False
    because "high" < "medium" alphabetically -- inverting any threshold a caller
    writes, silently and in the direction that accepts bad matches.
    """

    def test_ranks_ascend(self):
        assert Confidence.LOW < Confidence.MEDIUM < Confidence.HIGH

    def test_the_alphabetical_trap_does_not_bite(self):
        assert Confidence.HIGH > Confidence.MEDIUM
        assert Confidence.HIGH > Confidence.LOW
        assert not Confidence.HIGH < Confidence.LOW

    def test_sorts_by_rank_not_by_string(self):
        assert [c.name for c in sorted(Confidence)] == ["LOW", "MEDIUM", "HIGH"]

    def test_min_works_as_a_cap(self):
        """``apply_speculation`` relies on min() to clamp, so this must hold."""
        assert min(Confidence.HIGH, Confidence.MEDIUM) is Confidence.MEDIUM
        assert min(Confidence.LOW, Confidence.MEDIUM) is Confidence.LOW

    def test_still_a_string(self):
        assert Confidence("high") is Confidence.HIGH
        assert Confidence.HIGH == "high"
