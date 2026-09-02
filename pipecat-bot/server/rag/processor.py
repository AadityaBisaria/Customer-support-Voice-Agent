"""Pipeline processor that grounds each user turn in the Q&A corpus.

Speculative retrieval: embedding + search starts on *interim* transcripts,
while the user is still speaking, so by the time the final transcript lands
the gate decision is usually already cached — the turn pays ~0 added latency.
On a cache miss the inline search costs ~10-30 ms, still inside the turn's
transcript window.
"""

import asyncio
import re
from collections.abc import Callable

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    LLMMessagesTransformFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from language.processor import user_text_from_frame

from .gate import GateDecision, clear_grounding, decide, replace_grounding
from .retriever import Retriever

_WS = re.compile(r"[\W_]+", re.UNICODE)
_MAX_CACHE = 16


def _normalize(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


class RAGGroundingProcessor(FrameProcessor):
    """Retrieves grounding for each user turn, speculatively on interims."""

    def __init__(
        self,
        *,
        index: Retriever,
        top_k: int = 3,
        language_hint: Callable[[], str] | None = None,
        is_active: Callable[[], bool] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._index = index
        self._top_k = top_k
        self._language_hint = language_hint
        # None -> always active (the pre-flows behavior). When wired to the
        # flow graph, retrieval runs only at knowledge-capable nodes; on other
        # turns the stale [RAG] block is cleared instead.
        self._is_active = is_active
        self._cache: dict[str, GateDecision] = {}
        self._speculation: asyncio.Task | None = None

    def _active(self) -> bool:
        return self._is_active is None or self._is_active()

    def _search(self, text: str) -> GateDecision:
        query_vec = self._index.embed_query(text)
        return decide(self._index.search(query_vec, top_k=self._top_k))

    async def _speculate(self, normalized: str) -> None:
        decision = await asyncio.to_thread(self._search, normalized)
        self._remember(normalized, decision)
        logger.debug(
            "rag speculative top={:.2f} band={} for {!r}",
            decision.top_score,
            decision.band,
            normalized,
        )

    def _remember(self, normalized: str, decision: GateDecision) -> None:
        self._cache[normalized] = decision
        while len(self._cache) > _MAX_CACHE:
            self._cache.pop(next(iter(self._cache)))

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, InterimTranscriptionFrame):
            normalized = _normalize(frame.text)
            if self._active() and len(normalized.split()) >= 3 and normalized not in self._cache:
                if self._speculation and not self._speculation.done():
                    await self.cancel_task(self._speculation)
                self._speculation = self.create_task(self._speculate(normalized))

        elif (text := user_text_from_frame(frame)) is not None:
            if not self._active():
                await self.push_frame(
                    LLMMessagesTransformFrame(transform=clear_grounding()), direction
                )
                await self.push_frame(frame, direction)
                return
            normalized = _normalize(text)
            if normalized:
                decision = self._cache.get(normalized)
                hit = decision is not None
                if decision is None:
                    decision = await asyncio.to_thread(self._search, normalized)
                    self._remember(normalized, decision)
                logger.info(
                    "rag top={:.2f} band={} speculative_hit={} matches={}",
                    decision.top_score,
                    decision.band,
                    hit,
                    [e.id for e, _ in decision.matches[:3]],
                )
                hint = self._language_hint() if self._language_hint else None
                await self.push_frame(
                    LLMMessagesTransformFrame(
                        transform=replace_grounding(decision, language_hint=hint)
                    ),
                    direction,
                )

        await self.push_frame(frame, direction)
