"""Structured transactional conversations over pipecat.flows.

The LLM proposes (routing tools, enum selections); deterministic gates
dispose (slot classifiers, policy validators, confirm readbacks); code
executes (SupportStore mutations, called only from the confirm gate).

Depends on store ports/domain/policy — never on store.sqlite (test-enforced).
"""

from .gate_processor import GATE_STATE_KEY, SlotGate, SlotGateProcessor
from .nodes import make_greet_unauth, make_triage
from .pending import PendingMutation
from .session import SessionDeps

# Nodes where the caller may ask knowledge-base questions: the RAG processor
# runs only here. Everywhere else it clears stale grounding instead — a FLOOR
# "say you don't have that information" directive must never leak into a
# slot-collection turn.
RAG_ACTIVE_NODES = frozenset({"greet_unauth", "kb_only", "triage"})


def is_rag_active(flow_manager) -> bool:
    """None-safe: before the flow initializes, RAG stays active."""
    if flow_manager is None or flow_manager.current_node is None:
        return True
    return flow_manager.current_node in RAG_ACTIVE_NODES


async def build_initial_node(deps: SessionDeps, flow_manager) -> dict:
    """Twilio callers with a caller-ID match land verified in triage; everyone
    else (webrtc, eval, unmatched callers) starts at the old-bot-compatible
    greeting."""
    if deps.customer is not None:
        return await make_triage(deps, flow_manager, greet=True)
    return await make_greet_unauth(deps, flow_manager)


__all__ = [
    "GATE_STATE_KEY",
    "PendingMutation",
    "RAG_ACTIVE_NODES",
    "SessionDeps",
    "SlotGate",
    "SlotGateProcessor",
    "build_initial_node",
    "is_rag_active",
]
