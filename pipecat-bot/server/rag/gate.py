"""The deterministic three-way grounding gate.

Whether the bot may answer a knowledge question is decided here, by a cosine
threshold — never by the LLM. Below the floor, the injected instruction says
plainly that the knowledge base has no answer; the model's job is only to say
so in the caller's language mix.
"""

import os
from dataclasses import dataclass
from enum import StrEnum

from .index import QAEntry

SENTINEL = "[RAG]"


class GateBand(StrEnum):
    HIGH = "HIGH"  # confident match: answer strictly from it
    MID = "MID"  # plausible matches: answer only if one actually fits
    FLOOR = "FLOOR"  # no match: say we don't have it, do not invent


@dataclass(frozen=True)
class GateDecision:
    band: GateBand
    matches: tuple[tuple[QAEntry, float], ...]
    top_score: float


def _thresholds() -> tuple[float, float]:
    # Defaults from rag.tune_thresholds against the committed corpus:
    # HIGH sits above the strongest trap score (0.860), so no off-corpus
    # question can ever reach the "answer strictly" band; FLOOR is the trap
    # median. The MID zone between them relies on its deflection instruction.
    return (
        float(os.getenv("RAG_THRESHOLD_HIGH", "0.89")),
        float(os.getenv("RAG_THRESHOLD_FLOOR", "0.60")),
    )


def decide(
    matches: list[tuple[QAEntry, float]],
    *,
    high: float | None = None,
    floor: float | None = None,
) -> GateDecision:
    env_high, env_floor = _thresholds()
    high = env_high if high is None else high
    floor = env_floor if floor is None else floor

    top_score = matches[0][1] if matches else 0.0
    if not matches or top_score < floor:
        band = GateBand.FLOOR
    elif top_score >= high:
        band = GateBand.HIGH
    else:
        band = GateBand.MID
    return GateDecision(band=band, matches=tuple(matches), top_score=top_score)


def _render_qa(entry: QAEntry) -> str:
    return f"Q: {entry.question}\nA: {entry.answer}"


def grounding_message(decision: GateDecision, *, language_hint: str | None = None) -> str:
    """The [RAG] developer message injected for the current user question.

    ``language_hint`` is a one-line reply-language instruction from the
    language tracker; riding along in this per-turn message keeps it next to
    the generation point, where a small model actually follows it.
    """
    if decision.band is GateBand.FLOOR:
        body = (
            f"{SENTINEL} The knowledge base has NO answer to the user's question. "
            "If the user is asking you to DO something with their orders or "
            "account and a matching function is available, ignore this note and "
            "call that function. Otherwise say — in the user's current language "
            "mix — that you don't have that information, and offer to help with "
            "returns, refunds, replacements, or delivery questions instead. Do "
            "NOT invent an answer, a policy, a timeline, or an amount."
        )
    elif decision.band is GateBand.HIGH:
        entry = decision.matches[0][0]
        body = (
            f"{SENTINEL} Knowledge-base answer for the user's question. Answer "
            f"strictly and only from this; rephrase into the caller's language "
            f"mix but keep every fact exact:\n{_render_qa(entry)}"
        )
    else:
        blocks = "\n\n".join(_render_qa(entry) for entry, _ in decision.matches[:3])
        body = (
            f"{SENTINEL} Possibly relevant knowledge-base entries. Answer only if "
            f"one of these actually answers the user's question; otherwise say you "
            f"don't have that information. Do not mix entries or invent details:\n"
            f"{blocks}"
        )

    if language_hint:
        body += f"\nReply language: {language_hint}"
    return body


def clear_grounding():
    """Pure context transform: drop any [RAG] message, append nothing.

    Pushed when the RAG processor is inactive (mid-flow collect turns) so a
    stale FLOOR "say you don't have that information" directive from the last
    knowledge turn can never leak into a slot-collection turn.
    """

    def transform(messages: list[dict]) -> list[dict]:
        return [
            m
            for m in messages
            if not (
                m.get("role") == "developer"
                and isinstance(m.get("content"), str)
                and m["content"].startswith(SENTINEL)
            )
        ]

    return transform


def replace_grounding(decision: GateDecision, *, language_hint: str | None = None):
    """Pure context transform: drop the old [RAG] message, append the new one."""

    def transform(messages: list[dict]) -> list[dict]:
        kept = [
            m
            for m in messages
            if not (
                m.get("role") == "developer"
                and isinstance(m.get("content"), str)
                and m["content"].startswith(SENTINEL)
            )
        ]
        kept.append(
            {
                "role": "developer",
                "content": grounding_message(decision, language_hint=language_hint),
            }
        )
        return kept

    return transform
