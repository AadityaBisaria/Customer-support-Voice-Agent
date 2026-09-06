"""Pipecat adapter: one semantic invocation at most, then code-rendered speech."""

import asyncio
import json
import re
from time import monotonic

from loguru import logger
from pipecat.frames.frames import (
    CancelFrame,
    EndWorkerFrame,
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from .commands import COMPILER_PROMPT, CompilerBatch
from .runtime import CLARIFICATION, CommandRuntime


class CommandProcessor(FrameProcessor):
    def __init__(self, *, llm, deps, index, **kwargs):
        super().__init__(**kwargs)
        self.llm = llm
        self.runtime = CommandRuntime(deps)
        self.index = index
        self.generation = 0
        self.lock = asyncio.Lock()
        self._turn_started: dict[int, float] = {}

    async def speak(self, text, generation):
        if generation != self.generation or not text:
            return
        started = self._turn_started.get(generation)
        if started is not None:
            self.runtime.event(
                'text_response_started', turn_id=generation,
                elapsed_ms=round((monotonic() - started) * 1000),
            )
            # A turn may have a bridge followed by a fact readback. Only its
            # first response frame measures caller-perceived text latency.
            self._turn_started.pop(generation, None)
        self.runtime.last_speech = text
        # This is terminal, code-rendered speech for the current turn.  Do not
        # push an LLMContextFrame here: doing so would schedule a second model
        # completion after a deterministic fact readback or refusal.
        await self.push_frame(LLMFullResponseStartFrame())
        await self.push_frame(LLMTextFrame(text))
        await self.push_frame(LLMFullResponseEndFrame())

    async def greet(self):
        await self.speak('नमस्ते! मैं Aryan Retail assistant हूँ। मैं आपकी कैसे मदद कर सकता हूँ?',
                         self.generation)

    @staticmethod
    def _lexical_tokens(text):
        return set(re.findall(r'[a-z0-9]+', text.casefold()))

    @classmethod
    def _lexical_score(cls, entry, query_tokens):
        surfaces = (entry.question, *entry.paraphrases, *entry.tags)
        entry_tokens = set().union(*(cls._lexical_tokens(surface) for surface in surfaces))
        return len(query_tokens & entry_tokens) / max(1, len(query_tokens))

    @staticmethod
    def _exact_surface(entry, text):
        query = ' '.join(text.casefold().split())
        return any(query == ' '.join(surface.casefold().split())
                   for surface in (entry.question, *entry.paraphrases))

    @staticmethod
    def _policy_question(text):
        normalized = text.casefold()
        return any(phrase in normalized for phrase in (
            'policy', 'returnable', 'replacement only', 'refund rule',
            'refund policy', 'return rule', 'रिटर्न पॉलिसी', 'पॉलिसी',
        ))

    def retrieve(self, text):
        """Return one grounded policy source only when retrieval is decisive.

        The former 0.89 cut-off was calibrated as though query and corpus
        wording were identical.  It dropped ordinary Hinglish policy queries
        entirely.  A lower floor is safe only alongside a winner margin: the
        caller gets no source for an ambiguous lookup, and the model can only
        cite the single source that survived this gate.
        """
        if self.index is None:
            return {}
        results = self.index.search(self.index.embed_query(text), top_k=8)
        if not results:
            return {}
        query_tokens = self._lexical_tokens(text)
        # Semantic search supplies recall; a modest lexical component reranks
        # close candidates without letting one shared word override the
        # embedding score. Exact corpus surfaces remain the strongest signal.
        ranked = sorted(
            ((entry, semantic, semantic + 0.12 * self._lexical_score(entry, query_tokens))
             for entry, semantic in results),
            key=lambda candidate: candidate[2], reverse=True,
        )
        winner, winner_score, _ = ranked[0]
        runner_up_score = ranked[1][1] if len(ranked) > 1 else None
        floor = 0.60
        margin = 0.10
        if winner_score < floor:
            return {}
        if (runner_up_score is not None and winner_score - runner_up_score < margin and
                not self._exact_surface(winner, text)):
            return {}
        return {winner.id: winner.answer}

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (InterruptionFrame, CancelFrame)):
            self.generation += 1
        if isinstance(frame, LLMContextFrame):
            self.generation += 1
            messages = frame.context.get_messages()
            # Copy the current utterance/history: aggregator state can change in flight.
            history = [dict(m) for m in messages if m.get('role') in {'user', 'assistant'}][-6:]
            user = next((m.get('content') for m in reversed(history)
                         if m.get('role') == 'user'), None)
            if isinstance(user, str):
                self.create_task(self.handle(user, history, self.generation))
            return
        await self.push_frame(frame, direction)

    async def handle(self, text, history, generation):
        started = monotonic()
        self._turn_started[generation] = started
        calls = 0
        async with self.lock:
            if generation != self.generation:
                return
            try:
                shortcut_started = monotonic()
                speech = await self.runtime.shortcut(text)
                self.runtime.event(
                    'deterministic_stage', turn_id=generation,
                    elapsed_ms=round((monotonic() - shortcut_started) * 1000),
                    resolved=speech is not None,
                )
                if speech is None:
                    retrieval_started = monotonic()
                    sources = await asyncio.to_thread(self.retrieve, text)
                    self.runtime.event(
                        'retrieval_stage', turn_id=generation,
                        elapsed_ms=round((monotonic() - retrieval_started) * 1000),
                        sources=len(sources),
                    )
                    if generation != self.generation:
                        return
                    if not sources and self._policy_question(text):
                        self.runtime.event('policy_abstained', turn_id=generation)
                        speech = self.runtime.policy_abstention()
                        await self.speak(speech, generation)
                        return
                    prompt = {'state': self.runtime.snapshot(), 'recent_turns': history,
                              'policy_sources': sources, 'utterance': text}
                    calls = 1
                    compiler_started = monotonic()
                    response = await asyncio.wait_for(self.llm.run_inference(
                        LLMContext(messages=[{'role': 'user', 'content': json.dumps(
                            prompt, ensure_ascii=False, default=str)}]),
                        system_instruction=COMPILER_PROMPT,
                        max_tokens=1500), timeout=15)
                    self.runtime.event(
                        'compiler_stage', turn_id=generation,
                        elapsed_ms=round((monotonic() - compiler_started) * 1000),
                    )
                    if generation != self.generation:
                        return
                    batch = CompilerBatch.model_validate_json(response or '')
                    # Acknowledgements are allowed only after atomic command validation.
                    from copy import deepcopy

                    from .commands import ResolveIntent
                    # RESOLVE_INTENT is intentionally not a stack mutation:
                    # runtime resolves its raw phrase against the live store
                    # before it creates any internal frame.
                    if not any(isinstance(command, ResolveIntent) for command in batch.commands):
                        preview = deepcopy(self.runtime.stack)
                        preview.apply(batch)
                    bridge = batch.bridge_text
                    allowed_bridges = {'जी, मैं देख रहा हूँ।', 'ज़रूर, मैं check कर रहा हूँ।',
                                       "Sure, I'll check that for you."}
                    lookup = (self.runtime.deps.customer is not None and
                              any(isinstance(c, ResolveIntent) for c in batch.commands))
                    operation = self.create_task(self.runtime.apply(batch, sources=sources))
                    if lookup and bridge in allowed_bridges:
                        ready, _ = await asyncio.wait({operation}, timeout=0.12)
                        if not ready:
                            await self.speak(bridge, generation)
                    speech = await operation
                await self.speak(speech, generation)
                if self.runtime.ended and generation == self.generation:
                    await self.push_frame(EndWorkerFrame())
            except (ValueError, KeyError):
                logger.exception('Invalid semantic command or selection')
                await self.speak(CLARIFICATION, generation)
            except Exception:
                logger.exception('Command turn failed')
                await self.speak('अभी जानकारी check नहीं हो पाई। कृपया थोड़ी देर बाद फिर कोशिश कीजिए।',
                                 generation)
            finally:
                self._turn_started.pop(generation, None)
                self.runtime.event('turn_completed', turn_id=generation, llm_calls=calls,
                                   elapsed_ms=round((monotonic() - started) * 1000),
                                   stale=generation != self.generation)
