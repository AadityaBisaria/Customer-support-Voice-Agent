"""SlotGateProcessor: the deterministic gate in front of the LLM.

While a gate is armed (confirm node, phone-verify node), a final user turn is
fitted by a dialogue Slot BEFORE it can reach the aggregator:

- Fit(HIGH)  -> the frame is swallowed (no LLM run for this turn); the user's
  words are appended to context for transcript fidelity; the gate's callback
  decides what happens (execute the mutation, look up the phone number) and
  sets the next node, whose respond_immediately produces the bot's reply.
- Anything else -> the frame passes through. Gate nodes are TOOLLESS, so the
  LLM can only re-ask — an unclear turn can never mutate anything.

Interim frames always pass through: fit_utterance caps interim fits at
MEDIUM (speculation), which never commits.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from pipecat.frames.frames import Frame, LLMMessagesAppendFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from dialogue.results import Confidence, Fit
from dialogue.slots import Slot
from dialogue.utterance import Utterance
from language.processor import user_text_from_frame


@dataclass
class SlotGate:
    slot: Slot[Any]
    on_fit: Callable[[Any], Awaitable[None]]
    on_exhausted: Callable[[], Awaitable[None]] | None = None
    max_attempts: int = 0  # 0 = unlimited re-asks
    attempts: int = field(default=0)


GATE_STATE_KEY = "slot_gate"

Verdict = tuple[str, Any]  # ("fit", value) | ("exhausted", None) | ("pass", None)


async def evaluate_gate(gate: SlotGate, text: str, deps: Any) -> Verdict:
    """The gate decision, free of pipeline plumbing (unit-tested directly)."""
    result = await gate.slot.fit_utterance(Utterance.from_text(text), deps.fit_ctx(gate.attempts))
    if isinstance(result, Fit) and result.confidence is Confidence.HIGH:
        return ("fit", result.value)
    gate.attempts += 1
    if gate.max_attempts and gate.attempts >= gate.max_attempts and gate.on_exhausted:
        return ("exhausted", None)
    return ("pass", None)


class SlotGateProcessor(FrameProcessor):
    """Pipeline position: stt -> language_mirror -> THIS -> rag -> aggregator."""

    def __init__(self, *, get_flow_manager: Callable[[], Any], **kwargs) -> None:
        super().__init__(**kwargs)
        # Late-bound: the processor is constructed before the FlowManager.
        self._get_flow_manager = get_flow_manager

    def _gate(self) -> SlotGate | None:
        fm = self._get_flow_manager()
        if fm is None:
            return None
        return fm.state.get(GATE_STATE_KEY)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        text = user_text_from_frame(frame)
        gate = self._gate() if text is not None else None
        if gate is None:
            await self.push_frame(frame, direction)
            return

        fm = self._get_flow_manager()
        deps = fm.state["deps"]
        verdict, value = await evaluate_gate(gate, text, deps)
        logger.info("slot gate {}: {!r} -> {}", gate.slot.name, text, verdict)

        if verdict == "fit":
            fm.state.pop(GATE_STATE_KEY, None)  # callback may arm a new gate
            await self._record_turn(text, direction)
            await gate.on_fit(value)
            return  # swallow: the next node's response speaks for this turn

        if verdict == "exhausted":
            fm.state.pop(GATE_STATE_KEY, None)
            await self._record_turn(text, direction)
            await gate.on_exhausted()
            return

        # Pass through: the toolless gate node's task makes the LLM re-ask.
        await self.push_frame(frame, direction)

    async def _record_turn(self, text: str, direction: FrameDirection) -> None:
        """Keep the swallowed words in the transcript without running the LLM."""
        await self.push_frame(
            LLMMessagesAppendFrame(messages=[{"role": "user", "content": text}], run_llm=False),
            direction,
        )
