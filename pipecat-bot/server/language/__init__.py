"""Adaptive Hinglish language mirroring.

Measures the Hindi/English mix of each user utterance, tracks a smoothed
estimate across the call, and steers the LLM's reply mix to match via a
``[LANG-STYLE]`` developer directive that is swapped only when the user's
band actually changes.
"""

from .directives import SENTINEL, directive_for_band, replace_directive, short_hint_for_band
from .processor import LanguageMirrorProcessor
from .ratio import hindi_ratio
from .tracker import Band, LanguageBandTracker

__all__ = [
    "SENTINEL",
    "Band",
    "LanguageBandTracker",
    "LanguageMirrorProcessor",
    "directive_for_band",
    "hindi_ratio",
    "replace_directive",
    "short_hint_for_band",
]
