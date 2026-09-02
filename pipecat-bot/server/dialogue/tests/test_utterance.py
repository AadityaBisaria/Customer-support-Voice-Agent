"""Utterance normalization: the invariant the whole matching layer rests on."""

import dataclasses

import pytest
from pipecat.frames.frames import InterimTranscriptionFrame, TranscriptionFrame
from pipecat.utils.text.alnum_utils import fold_for_matching

from dialogue import Utterance, normalize


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("let's do Wednesday", ("lets", "do", "wednesday")),
        ("The TEN one, please.", ("the", "ten", "one", "please")),
        ("Actually, make it a follow-up", ("actually", "make", "it", "a", "followup")),
        ("  spaced   out  ", ("spaced", "out")),
        ("...", ()),
        ("", ()),
    ],
)
def test_normalize_worked_examples(text, expected):
    assert normalize(text) == expected


@pytest.mark.parametrize(
    ("spoken", "surface"),
    [
        ("let's", "lets"),
        ("follow-up", "followup"),
        ("Wednesday", "wednesday"),
        ("CAFE!", "cafe"),
    ],
)
def test_normalization_invariant(spoken, surface):
    """A surface form and a transcript spelling it differently must normalize equal.

    This is the rule that lets a slot's surface map be written in natural spelling
    while still matching whatever STT emits. If it ever breaks, every enum slot
    silently stops matching.
    """
    assert normalize(spoken) == normalize(surface)


@pytest.mark.parametrize(
    "text",
    ["let's do Wednesday", "cafe at 3pm?", "plain ascii", "", "     ", "Ünïcödé accents"],
)
def test_fold_preserves_length(text):
    """Span offsets index into ``raw``, which is only sound if folding is 1:1."""
    u = Utterance.from_text(text)
    assert len(u.folded) == len(u.raw) == len(text)
    assert u.folded == fold_for_matching(text)


def test_token_set_matches_tokens():
    u = Utterance.from_text("the ten one please")
    assert u.token_set == set(u.tokens)
    assert "ten" in u
    assert "wednesday" not in u


class TestPhraseMatching:
    """A set cannot match a multi-token surface form; the ordered tuple must."""

    @pytest.mark.parametrize(
        "text",
        ["the first one", "give me the first one", "the first one please"],
    )
    def test_consecutive_phrase_matches(self, text):
        assert Utterance.from_text(text).has_phrase(normalize("the first one"))

    @pytest.mark.parametrize(
        "text",
        [
            "one thing first, the end",  # all three tokens, wrong order
            "the one that came first",  # all three tokens, not consecutive
            "the first",  # prefix only
            "first one",  # missing leading token
        ],
    )
    def test_scattered_or_partial_does_not_match(self, text):
        assert not Utterance.from_text(text).has_phrase(normalize("the first one"))

    def test_empty_phrase_never_matches(self):
        assert not Utterance.from_text("anything").has_phrase(())


class TestFromFrame:
    """Finality comes from the frame TYPE, never from ``finalized``."""

    def test_final_frame(self):
        u = Utterance.from_frame(
            TranscriptionFrame(text="let's do Wednesday", user_id="u", timestamp="t")
        )
        assert u.is_final
        assert u.tokens == ("lets", "do", "wednesday")

    def test_interim_frame(self):
        u = Utterance.from_frame(
            InterimTranscriptionFrame(text="let's do wed", user_id="u", timestamp="t")
        )
        assert not u.is_final

    def test_finalized_field_is_not_the_discriminator(self):
        """``finalized`` defaults False even on a final frame, so it must not be used."""
        frame = TranscriptionFrame(text="hello", user_id="u", timestamp="t")
        assert frame.finalized is False
        assert Utterance.from_frame(frame).is_final is True

    def test_frame_classes_are_siblings(self):
        """The isinstance check is only sound while neither subclasses the other."""
        assert not issubclass(InterimTranscriptionFrame, TranscriptionFrame)
        assert not issubclass(TranscriptionFrame, InterimTranscriptionFrame)

    def test_t_recv_is_monotonic_not_frame_timestamp(self):
        """Frame timestamps are ISO8601 strings; timing must use our own clock."""
        u = Utterance.from_frame(
            TranscriptionFrame(text="hi", user_id="u", timestamp="not-a-number")
        )
        assert isinstance(u.t_recv, float)
        assert u.t_recv > 0


def test_utterance_is_frozen():
    u = Utterance.from_text("hello")
    with pytest.raises(dataclasses.FrozenInstanceError):
        u.raw = "goodbye"  # type: ignore[misc]
