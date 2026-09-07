"""LanguageBandTracker: EMA math, band transitions, hysteresis."""

import pytest

from language.tracker import Band, LanguageBandTracker


def test_starts_in_hinglish():
    tracker = LanguageBandTracker()
    assert tracker.band is Band.HINGLISH
    assert tracker.ema == pytest.approx(0.5)


def test_ema_update():
    tracker = LanguageBandTracker(alpha=0.4, initial=0.5)
    # A faint Hindi contribution remains smoothed; clearly mixed speech is
    # intentionally an immediate Hinglish preference signal.
    tracker.observe(0.3)
    assert tracker.ema == pytest.approx(0.4 * 0.3 + 0.6 * 0.5)


def test_none_does_not_move_ema_or_band():
    tracker = LanguageBandTracker()
    assert tracker.observe(None) is None
    assert tracker.ema == pytest.approx(0.5)
    assert tracker.band is Band.HINGLISH


def test_transition_to_mostly_hindi_fires_once():
    tracker = LanguageBandTracker(alpha=0.4, initial=0.5)
    # Hindi-led speech deliberately uses the product's Hinglish style.
    assert tracker.observe(1.0) is None
    assert tracker.band is Band.HINGLISH
    assert tracker.observe(1.0) is None  # already there: no re-fire


def test_transition_to_mostly_english():
    tracker = LanguageBandTracker(alpha=0.5, initial=0.5)
    assert tracker.observe(0.0) is Band.MOSTLY_ENGLISH
    assert tracker.band is Band.MOSTLY_ENGLISH


def test_hindi_led_turn_leaves_english_for_hinglish_immediately():
    tracker = LanguageBandTracker()
    tracker.observe(0.0)
    # Equivalent to: मुझे अपने running shoes return करने हैं.
    assert tracker.observe(0.57) is Band.HINGLISH
    assert tracker.band is Band.HINGLISH


def test_no_flapping_around_a_boundary():
    """Oscillating raw ratios near 0.65 must not toggle the band every turn."""
    tracker = LanguageBandTracker(alpha=0.4, initial=0.72)
    tracker._band = Band.MOSTLY_HINDI
    changes = [tracker.observe(r) for r in [0.6, 0.7, 0.6, 0.7, 0.6, 0.7]]
    assert sum(c is not None for c in changes) <= 1


def test_extreme_swing_crosses_two_bands():
    tracker = LanguageBandTracker(alpha=1.0, initial=0.5)  # alpha=1: no smoothing
    assert tracker.observe(0.05) is Band.MOSTLY_ENGLISH
    assert tracker.observe(0.95) is Band.HINGLISH
