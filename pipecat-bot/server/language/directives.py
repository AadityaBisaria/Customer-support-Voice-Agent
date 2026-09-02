"""Language-style directives injected into the LLM context.

Each band maps to a developer message starting with the ``[LANG-STYLE]``
sentinel. ``replace_directive`` builds the pure context transform that swaps
the old directive for the new one — the context never accumulates stale
directives.

The few-shot lines matter: small local models follow a worked example far
more reliably than an abstract instruction. Hindi is written in Devanagari
so bulbul:v3 pronounces it natively.
"""

from typing import Any

from .tracker import Band

SENTINEL = "[LANG-STYLE]"

_DIRECTIVES = {
    Band.MOSTLY_ENGLISH: (
        f"{SENTINEL} The caller is speaking mostly English. Reply in plain "
        "English only — no Hindi words. Example:\n"
        "User: I want to return my order, it arrived damaged.\n"
        "Assistant: Sorry about that! You can return it from the Your Orders "
        "page — just pick the item and choose a return reason."
    ),
    Band.HINGLISH: (
        f"{SENTINEL} The caller is speaking Hinglish — a natural mix of Hindi "
        "and English. Mirror that mix: switch between Hindi and English "
        "mid-sentence the way they do. ALWAYS write your Hindi words in "
        "Devanagari script — even when the caller's Hindi appears in Latin "
        "letters — so they are pronounced correctly, and keep product or "
        "technical terms (order, refund, return, pickup) in English. These "
        "examples show the LANGUAGE STYLE only — what you actually do or say "
        "always comes from the current task:\n"
        "User: thoda dhire boliye please\n"
        "Assistant: ज़रूर! Main थोड़ा आराम से बोलती हूँ, बताइए.\n"
        "User: aap kaun ho?\n"
        "Assistant: Main एक demo support assistant हूँ, आपके सवालों में help "
        "के लिए."
    ),
    Band.MOSTLY_HINDI: (
        f"{SENTINEL} The caller is speaking mostly Hindi. Reply almost "
        "entirely in Hindi, written in Devanagari script so it is pronounced "
        "correctly. Keep only product or technical terms (order, refund, "
        "return, pickup) in English. This example shows the LANGUAGE STYLE "
        "only — what you actually do or say always comes from the current "
        "task:\n"
        "User: आप क्या कर सकती हैं?\n"
        "Assistant: मैं returns, refunds और delivery के सवालों में मदद कर "
        "सकती हूँ."
    ),
}


_SHORT_HINTS = {
    Band.MOSTLY_ENGLISH: "Reply in plain English only — no Hindi words.",
    Band.HINGLISH: ("Reply in Hinglish: mix Hindi (written in Devanagari) with English."),
    Band.MOSTLY_HINDI: (
        "Reply almost entirely in Hindi written in Devanagari, keeping "
        "technical terms like order or refund in English."
    ),
}


def directive_for_band(band: Band) -> str:
    """The [LANG-STYLE] directive text for a band."""
    return _DIRECTIVES[band]


def short_hint_for_band(band: Band) -> str:
    """One-line reply-language hint, embedded into each turn's [RAG] message.

    A small model follows the instruction closest to the generation point far
    more reliably than an early-context directive, especially once the
    conversation history is dominated by the previous band's style.
    """
    return _SHORT_HINTS[band]


def replace_directive(band: Band):
    """Pure context transform: swap the [LANG-STYLE] message for the band's.

    The directive is inserted right after any leading system message(s) — NOT
    appended at the end — so it reads as durable guidance. Small models
    sometimes echo an instruction-shaped message verbatim when it sits close
    to the generation point.
    """

    def transform(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept = [
            m
            for m in messages
            if not (
                m.get("role") == "developer"
                and isinstance(m.get("content"), str)
                and m["content"].startswith(SENTINEL)
            )
        ]
        insert_at = 0
        while insert_at < len(kept) and kept[insert_at].get("role") == "system":
            insert_at += 1
        kept.insert(insert_at, {"role": "developer", "content": directive_for_band(band)})
        return kept

    return transform
