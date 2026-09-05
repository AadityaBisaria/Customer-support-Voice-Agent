"""Pipecat adapter: one semantic invocation at most, then code-rendered speech."""

import asyncio
import json
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

from .commands import COMPILER_PROMPT, CommandBatch
from .runtime import CLARIFICATION, CommandRuntime


class CommandProcessor(FrameProcessor):
    def __init__(self, *, llm, deps, index, **kwargs):
        super().__init__(**kwargs)
        self.llm = llm
        self.runtime = CommandRuntime(deps)
        self.index = index
        self.generation = 0
        self.lock = asyncio.Lock()

    async def speak(self, text, generation):
        if generation != self.generation or not text:
            return
        self.runtime.last_speech = text
        await self.push_frame(LLMFullResponseStartFrame())
        await self.push_frame(LLMTextFrame(text))
        await self.push_frame(LLMFullResponseEndFrame())

    async def greet(self):
        await self.speak('नमस्ते! मैं Aryan Retail assistant हूँ। मैं आपकी कैसे मदद कर सकता हूँ?',
                         self.generation)

    def retrieve(self, text):
        return {entry.id: entry.answer for entry, score in
                self.index.search(self.index.embed_query(text), top_k=3) if score >= 0.89}

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
        calls = 0
        async with self.lock:
            if generation != self.generation:
                return
            try:
                speech = await self.runtime.shortcut(text)
                if speech is None:
                    sources = await asyncio.to_thread(self.retrieve, text)
                    if generation != self.generation:
                        return
                    prompt = {'state': self.runtime.snapshot(), 'recent_turns': history,
                              'policy_sources': sources, 'utterance': text}
                    calls = 1
                    response = await asyncio.wait_for(self.llm.run_inference(
                        LLMContext(messages=[{'role': 'user', 'content': json.dumps(
                            prompt, ensure_ascii=False, default=str)}]),
                        system_instruction=COMPILER_PROMPT,
                        max_tokens=1500), timeout=15)
                    if generation != self.generation:
                        return
                    batch = CommandBatch.model_validate_json(response or '')
                    # Acknowledgements are allowed only after atomic command validation.
                    from copy import deepcopy

                    from .commands import SetSlot, StartFlow
                    preview = deepcopy(self.runtime.stack)
                    preview.apply(batch)
                    bridge = batch.bridge_text
                    allowed_bridges = {'जी, मैं देख रहा हूँ।', 'ज़रूर, मैं check कर रहा हूँ।',
                                       "Sure, I'll check that for you."}
                    lookup = (self.runtime.deps.customer is not None and
                              any(isinstance(c, (StartFlow, SetSlot)) for c in batch.commands))
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
                self.runtime.event('turn_completed', turn_id=generation, llm_calls=calls,
                                   elapsed_ms=round((monotonic() - started) * 1000),
                                   stale=generation != self.generation)
