"""clear_grounding transform + Retriever protocol conformance."""

from rag.gate import SENTINEL, clear_grounding
from rag.index import QAIndex
from rag.retriever import Retriever

from .test_index import fake_embed


def test_clear_grounding_drops_only_rag_messages():
    messages = [
        {"role": "system", "content": "persona"},
        {"role": "developer", "content": "[LANG-STYLE] directive"},
        {"role": "developer", "content": f"{SENTINEL} stale grounding"},
        {"role": "user", "content": "AMZ-1003 wala"},
    ]
    result = clear_grounding()(messages)
    assert result == [messages[0], messages[1], messages[3]]


def test_clear_grounding_is_noop_without_rag_message():
    messages = [{"role": "user", "content": "hi"}]
    assert clear_grounding()(messages) == messages


def test_qaindex_conforms_to_retriever_protocol():
    index = QAIndex([], embed_fn=fake_embed)
    assert isinstance(index, Retriever)
