"""Dynamic, per-caller order-reference fitting used before semantic compilation."""

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

_DIGITS = {
    'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4',
    'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
}
_STOP_WORDS = frozenset({
    'my', 'mera', 'mere', 'meri', 'apna', 'apne', 'apni', 'mujhe', 'मैं', 'मेरा',
    'मेरे', 'मेरी', 'अपना', 'अपने', 'मुझे', 'the', 'order', 'orders', 'cancel',
    'return', 'exchange', 'status', 'tracking', 'please', 'ka', 'ki', 'ke', 'hai',
    'karna', 'chahiye', 'item',
})
_ORDINALS = {
    1: frozenset({'first', 'pehla', 'पहला', '1st', 'one', 'ek'}),
    2: frozenset({'second', 'doosra', 'दूसरा', '2nd', 'two', 'do'}),
    3: frozenset({'third', 'teesra', 'तीसरा', '3rd', 'three', 'teen'}),
    4: frozenset({'fourth', 'chautha', 'चौथा', '4th', 'four', 'chaar'}),
}
_RECENCY = frozenset({'latest', 'recent', 'newest', 'last', 'aakhri', 'नया', 'आखिरी'})


# A small, dependency-free Devanagari romanizer is deliberately used only for
# matching a caller's words to their *own live catalogue*. It is not a product
# synonym table: live titles supply the dynamic searchable vocabulary. Final
# vowel stemming bridges normal Hindi inflections such as कुर्ते -> kurte and
# an English title kurta.
_DEVANAGARI_CONSONANTS = {
    'क': 'k', 'ख': 'kh', 'ग': 'g', 'घ': 'gh', 'ङ': 'ng', 'च': 'ch', 'छ': 'chh',
    'ज': 'j', 'झ': 'jh', 'ञ': 'ny', 'ट': 't', 'ठ': 'th', 'ड': 'd', 'ढ': 'dh',
    'ण': 'n', 'त': 't', 'थ': 'th', 'द': 'd', 'ध': 'dh', 'न': 'n', 'प': 'p',
    'फ': 'ph', 'ब': 'b', 'भ': 'bh', 'म': 'm', 'य': 'y', 'र': 'r', 'ल': 'l',
    'व': 'v', 'श': 'sh', 'ष': 'sh', 'स': 's', 'ह': 'h', 'ड़': 'r', 'ढ़': 'rh',
    'क़': 'q', 'ख़': 'kh', 'ग़': 'gh', 'ज़': 'z', 'फ़': 'f', 'य़': 'y',
}
_DEVANAGARI_VOWELS = {
    'अ': 'a', 'आ': 'a', 'इ': 'i', 'ई': 'i', 'उ': 'u', 'ऊ': 'u', 'ऋ': 'ri',
    'ए': 'e', 'ऐ': 'ai', 'ओ': 'o', 'औ': 'au',
}
_DEVANAGARI_SIGNS = {
    'ा': 'a', 'ि': 'i', 'ी': 'i', 'ु': 'u', 'ू': 'u', 'ृ': 'ri', 'े': 'e',
    'ै': 'ai', 'ो': 'o', 'ौ': 'au', 'ं': 'n', 'ँ': 'n', 'ः': 'h',
}
_HINDI_GRAMMAR = frozenset({
    'mai', 'main', 'mera', 'mere', 'meri', 'mujhe', 'mujh', 'apna', 'apne', 'apni', 'apan',
    'ka', 'ki', 'ke', 'ko', 'se', 'par', 'aur', 'yeh', 'yah', 'hai', 'tha', 'the',
    'karna', 'karni', 'karo', 'karan', 'chahiye', 'nahi', 'nahin', 'please',
})


def _word_runs(text: str) -> list[str]:
    """Split without discarding Devanagari combining vowel signs."""
    words: list[str] = []
    current: list[str] = []
    for char in text.casefold():
        if char.isalnum() or unicodedata.category(char)[0] in {'L', 'M'}:
            current.append(char)
        elif current:
            words.append(''.join(current))
            current = []
    if current:
        words.append(''.join(current))
    return words


def _romanize_devanagari(word: str) -> str:
    output: list[str] = []
    chars = list(word)
    for index, char in enumerate(chars):
        if char in _DEVANAGARI_CONSONANTS:
            output.append(_DEVANAGARI_CONSONANTS[char])
            next_char = chars[index + 1] if index + 1 < len(chars) else ''
            if next_char not in _DEVANAGARI_SIGNS and next_char != '्':
                output.append('a')
        elif char in _DEVANAGARI_VOWELS:
            output.append(_DEVANAGARI_VOWELS[char])
        elif char in _DEVANAGARI_SIGNS:
            output.append(_DEVANAGARI_SIGNS[char])
        elif char.isascii() and char.isalnum():
            output.append(char)
    return ''.join(output)


def _stem(token: str) -> str:
    token = token.casefold()
    return token[:-1] if len(token) > 4 and token[-1:] in {'a', 'e', 'i', 'o', 'u'} else token


def reference_tokens(text: str) -> set[str]:
    """Normalized searchable terms from Latin or Devanagari caller input."""
    tokens = set()
    for word in _word_runs(text):
        romanized = _romanize_devanagari(word)
        raw = (romanized or word).casefold()
        token = _stem(raw)
        if (token and raw not in _STOP_WORDS and raw not in _HINDI_GRAMMAR and
                token not in _STOP_WORDS and token not in _HINDI_GRAMMAR):
            tokens.add(token)
    return tokens


def compact(text: str) -> str:
    return re.sub(r'[^a-z0-9]', '', text.casefold())


def canonical_order_id(text: str) -> str | None:
    """Accept AMZ-1004, AMZ 1004, amz1004, and spoken English digits."""
    words = re.findall(r'[a-z0-9]+', text.casefold())
    translated = ''.join(_DIGITS.get(word, word) for word in words)
    match = re.search(r'amz(\d+)', translated)
    return f'AMZ-{match.group(1)}' if match else None


@dataclass(frozen=True)
class OrderCandidate:
    order_id: str
    titles: tuple[str, ...]
    title_keywords: frozenset[str]
    placed_at: datetime
    status: str
    presentation_index: int | None = None

    @property
    def label(self) -> str:
        return ', '.join(self.titles)


@dataclass(frozen=True)
class OrderReferenceFit:
    """One canonical match, or candidates requiring a focused clarification."""
    order_id: str | None
    ambiguous: tuple[OrderCandidate, ...] = ()

    @property
    def matched(self) -> bool:
        return self.order_id is not None


class DynamicOrderReferenceSlot:
    """Closed-set matcher compiled from one authenticated caller's live orders."""

    def __init__(self, orders: Iterable, *, presentation_ids: Sequence[str] | None = None):
        presentation = {order_id: index for index, order_id in enumerate(presentation_ids or (), start=1)}
        chronological = sorted(orders, key=lambda order: order.placed_at, reverse=True)
        self.candidates = tuple(
            OrderCandidate(
                order_id=str(order.order_id),
                titles=tuple(order.item_titles),
                title_keywords=frozenset(
                    token for title in order.item_titles
                    for token in reference_tokens(title)
                ),
                placed_at=order.placed_at,
                status=order.status.value,
                presentation_index=presentation.get(str(order.order_id)),
            )
            for order in chronological
        )

    @classmethod
    def from_candidates(cls, candidates: Iterable[OrderCandidate]):
        slot = cls.__new__(cls)
        slot.candidates = tuple(candidates)
        return slot

    def match(self, utterance: str) -> OrderReferenceFit:
        canonical = canonical_order_id(utterance)
        if canonical:
            exact = tuple(candidate for candidate in self.candidates
                          if candidate.order_id.casefold() == canonical.casefold())
            return self._one_or_many(exact)

        words = reference_tokens(utterance)
        # "First" means first in the menu the caller heard. "Latest" remains
        # chronological, even when no menu has been presented.
        for position, markers in _ORDINALS.items():
            if words & markers:
                presented = tuple(candidate for candidate in self.candidates
                                  if candidate.presentation_index == position)
                if presented:
                    return self._one_or_many(presented)
        if words & _RECENCY and self.candidates:
            return OrderReferenceFit(self.candidates[0].order_id)

        flat = compact(utterance)
        hits: list[OrderCandidate] = []
        for candidate in self.candidates:
            title_hit = any(
                len(compact(title)) > 3 and compact(title) in flat
                for title in candidate.titles
            )
            keyword_hit = bool(words and candidate.title_keywords & words)
            if title_hit or keyword_hit:
                hits.append(candidate)
        return self._one_or_many(tuple(hits))

    @staticmethod
    def _one_or_many(candidates: tuple[OrderCandidate, ...]) -> OrderReferenceFit:
        if len(candidates) == 1:
            return OrderReferenceFit(candidates[0].order_id)
        return OrderReferenceFit(None, candidates)


def order_reference_matches(text: str, orders: Iterable) -> list[str]:
    """Compatibility helper for existing pre-actions; never invents an ID."""
    fit = DynamicOrderReferenceSlot(orders).match(text)
    return [fit.order_id] if fit.order_id else []
