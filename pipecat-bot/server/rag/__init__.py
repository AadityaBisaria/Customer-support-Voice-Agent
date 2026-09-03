"""Near-zero-latency RAG over the Aryan Retail help-page Q&A corpus.

An in-process embedding index (no vector DB) searched speculatively on
interim transcripts, with a deterministic three-way gate that decides how the
LLM may use the results — including an explicit "no answer, do not invent"
branch that is the primary anti-fabrication mechanism.
"""

from .gate import GateBand, GateDecision, decide, grounding_message, replace_grounding
from .index import QAEntry, QAIndex
from .processor import RAGGroundingProcessor

__all__ = [
    "GateBand",
    "GateDecision",
    "QAEntry",
    "QAIndex",
    "RAGGroundingProcessor",
    "decide",
    "grounding_message",
    "replace_grounding",
]
