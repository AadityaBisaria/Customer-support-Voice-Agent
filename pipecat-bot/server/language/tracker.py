"""Rolling language-band tracking: EMA over per-turn ratios, with hysteresis.

Pure state machine, no pipecat imports. The tracker smooths noisy per-turn
ratios and reports a band transition only when the smoothed estimate crosses
a band boundary — with different enter/exit thresholds so a caller hovering
near a boundary doesn't flap the directive every turn.
"""

from enum import StrEnum


class Band(StrEnum):
    """How much Hindi the bot should speak."""

    MOSTLY_ENGLISH = "MOSTLY_ENGLISH"
    HINGLISH = "HINGLISH"
    MOSTLY_HINDI = "MOSTLY_HINDI"


# Hysteresis boundaries on the smoothed ratio.
_ENTER_ENGLISH = 0.20  # drop below this -> MOSTLY_ENGLISH
_EXIT_ENGLISH = 0.30  # rise above this to leave MOSTLY_ENGLISH
_ENTER_HINDI = 0.70  # rise above this -> MOSTLY_HINDI
_EXIT_HINDI = 0.60  # drop below this to leave MOSTLY_HINDI


class LanguageBandTracker:
    """EMA of the user's Hindi ratio, quantized to a band with hysteresis."""

    def __init__(self, *, alpha: float = 0.5, initial: float = 0.5) -> None:
        self._alpha = alpha
        self._initial = initial
        self._ema = initial
        self._band = Band.HINGLISH

    def reset(self) -> None:
        """Back to the starting estimate — a new call is a new caller."""
        self._ema = self._initial
        self._band = Band.HINGLISH

    @property
    def ema(self) -> float:
        return self._ema

    @property
    def band(self) -> Band:
        return self._band

    def observe(self, ratio: float | None) -> Band | None:
        """Fold one turn's ratio in; return the new band only when it changes.

        ``None`` ratios (empty/backchannel turns) leave the estimate untouched.
        """
        if ratio is None:
            return None

        self._ema = self._alpha * ratio + (1 - self._alpha) * self._ema

        new_band = self._band
        if self._band is Band.MOSTLY_ENGLISH:
            if self._ema > _ENTER_HINDI:
                new_band = Band.MOSTLY_HINDI
            elif self._ema > _EXIT_ENGLISH:
                new_band = Band.HINGLISH
        elif self._band is Band.MOSTLY_HINDI:
            if self._ema < _ENTER_ENGLISH:
                new_band = Band.MOSTLY_ENGLISH
            elif self._ema < _EXIT_HINDI:
                new_band = Band.HINGLISH
        else:  # HINGLISH
            if self._ema < _ENTER_ENGLISH:
                new_band = Band.MOSTLY_ENGLISH
            elif self._ema > _ENTER_HINDI:
                new_band = Band.MOSTLY_HINDI

        if new_band is self._band:
            return None
        self._band = new_band
        return new_band
