"""hindi_ratio: script detection, romanized wordlist, and edge cases."""

import pytest

from language.ratio import ROMAN_HINDI_WORDS, hindi_ratio


@pytest.mark.parametrize(
    "text, expected",
    [
        # Pure English -> 0.0
        ("I want to return my order please", 0.0),
        # Pure Devanagari -> 1.0
        ("मेरा सामान अभी तक नहीं आया", 1.0),
        # Romanized Hindi only -> 1.0 (theek, hai, kal, aana all in wordlist)
        ("theek hai kal aana", 1.0),
        # Classic Hinglish: mera/aaya/hai Hindi, order/damaged English -> 3/5
        ("mera order damaged aaya hai", 3 / 5),
        # Mixed script: Devanagari tokens + English tokens
        ("रिफंड kab milega for my order", 3 / 6),
    ],
)
def test_ratio_values(text: str, expected: float):
    assert hindi_ratio(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "   ", "...!?", "12 34"])
def test_no_words_returns_none(text: str):
    """Empty/punctuation/number-only turns must not move a rolling estimate."""
    assert hindi_ratio(text) is None


def test_digits_are_not_counted_as_tokens():
    # "500" is ignored; rupaye/wapas/chahiye are Hindi, only "refund" is English.
    assert hindi_ratio("500 rupaye wapas chahiye refund") == pytest.approx(3 / 4)


def test_english_collisions_stay_english():
    """Greetings and function words that look vaguely Hindi must not count."""
    assert hindi_ratio("hi can you help me with the return") == 0.0


@pytest.mark.parametrize("word", ["hi", "the", "main", "to", "do", "me", "us"])
def test_collision_prone_words_absent_from_wordlist(word: str):
    assert word not in ROMAN_HINDI_WORDS


def test_devanagari_anywhere_in_token_counts_hindi():
    # A token mixing scripts (ASR artifact) still reads as Hindi.
    assert hindi_ratio("orderकहाँ") == 1.0
