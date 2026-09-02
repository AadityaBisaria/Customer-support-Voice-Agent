"""The normalized form of one user utterance, computed once per turn.

Every matcher in the cascade -- and the digression check after them -- reads this
same object, so folding and tokenization happen once rather than once per layer.
"""

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass

from pipecat.frames.frames import InterimTranscriptionFrame, TranscriptionFrame
from pipecat.utils.text.alnum_utils import alnum_only, fold_for_matching

_WORD = re.compile(r"\S+")


def _tokenize(folded: str) -> tuple[tuple[str, ...], tuple[tuple[int, int], ...]]:
    """Split folded text into tokens, keeping each token's char span.

    Splits on whitespace rather than on runs of alphanumerics, so an internal
    apostrophe does not fracture a word: "let's" is one token "lets", not "let"
    and "s". Each span is trimmed to the token's first and last alphanumeric
    character, so trailing punctuation is excluded from the reported extent.

    Spans index into the folded string, which -- by the 1:1 contract of
    ``fold_for_matching`` -- means they index the raw string identically.
    """
    tokens: list[str] = []
    spans: list[tuple[int, int]] = []
    for match in _WORD.finditer(folded):
        word = match.group()
        token = alnum_only(word)
        if not token:
            continue
        lead = next(i for i, c in enumerate(word) if c.isalnum())
        trail = next(i for i, c in enumerate(reversed(word)) if c.isalnum())
        tokens.append(token)
        spans.append((match.start() + lead, match.end() - trail))
    return tuple(tokens), tuple(spans)


def normalize(text: str) -> tuple[str, ...]:
    """Fold and tokenize text for matching.

    ``fold_for_matching`` folds case, accents and typographic punctuation while
    preserving length 1:1; ``alnum_only`` then strips each token to bare
    alphanumerics, which is what turns "let's" into "lets" and "follow-up" into
    "followup".

    This function is the normalization invariant of the whole slot layer: a slot's
    surface forms must be normalized through *this* function at construction time,
    or a transcript saying "let's" will silently fail to match a surface form
    spelled "lets". One normalizer, both sides.

    >>> normalize("let's do Wednesday")
    ('lets', 'do', 'wednesday')
    """
    return _tokenize(fold_for_matching(text))[0]


@dataclass(frozen=True, slots=True)
class Utterance:
    """One user utterance, folded and tokenized once for the whole cascade."""

    raw: str
    """Exactly what STT produced. Spans index into this."""

    folded: str
    """Case/accent/typography folded, same length as ``raw`` so offsets transfer."""

    tokens: tuple[str, ...]
    """Ordered, so multi-token surface forms can be matched as phrases."""

    token_set: frozenset[str]
    """The same tokens as a set, for O(1) prefiltering before phrase matching."""

    spans: tuple[tuple[int, int], ...]
    """``spans[i]`` is the char extent of ``tokens[i]`` in ``raw``. Same length as
    ``tokens``. This is what lets a matcher report which words justified a value."""

    is_final: bool
    """False for an interim transcript: match speculatively, never commit."""

    t_recv: float
    """``time.monotonic()`` at receipt. Pipecat's frame timestamps are ISO8601
    strings, so latency math needs our own clock."""

    @classmethod
    def from_text(cls, text: str, *, is_final: bool = True) -> "Utterance":
        """Build from a bare string. The path tests and offline tools use."""
        folded = fold_for_matching(text)
        tokens, spans = _tokenize(folded)
        return cls(
            raw=text,
            folded=folded,
            tokens=tokens,
            token_set=frozenset(tokens),
            spans=spans,
            is_final=is_final,
            t_recv=time.monotonic(),
        )

    @classmethod
    def from_frame(cls, frame: TranscriptionFrame | InterimTranscriptionFrame) -> "Utterance":
        """Build from a Pipecat transcription frame.

        Finality is decided by the frame's type, not by ``TranscriptionFrame.finalized``:
        that field defaults to False even on a final frame and is only set by STT
        services supporting explicit commit, so it would misclassify most finals.
        The two frame classes are siblings -- neither subclasses the other -- so the
        isinstance check is unambiguous.
        """
        return cls.from_text(frame.text, is_final=isinstance(frame, TranscriptionFrame))

    def __contains__(self, token: str) -> bool:
        """``"wednesday" in utterance`` -- the cheap single-token prefilter."""
        return token in self.token_set

    def find_phrase(self, phrase: Sequence[str], start: int = 0) -> int:
        """Token index where ``phrase`` occurs consecutively, or -1.

        Ordered matching, because a set cannot match a multi-token surface form:
        "the first one" normalizes to three tokens, and set-intersection would
        just as happily match "one first the", or those words scattered across an
        unrelated sentence.
        """
        phrase = tuple(phrase)
        n = len(phrase)
        if not n:
            return -1
        for i in range(start, len(self.tokens) - n + 1):
            if self.tokens[i : i + n] == phrase:
                return i
        return -1

    def has_phrase(self, phrase: Sequence[str]) -> bool:
        """True if the ordered tokens appear consecutively."""
        return self.find_phrase(phrase) >= 0

    def span_of(self, start: int, length: int) -> tuple[int, int]:
        """Char extent in ``raw`` covering ``tokens[start : start + length]``."""
        return (self.spans[start][0], self.spans[start + length - 1][1])
