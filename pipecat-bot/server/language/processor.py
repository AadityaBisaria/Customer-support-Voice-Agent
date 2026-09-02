"""Pipeline processor that mirrors the caller's Hindi/English mix.

Sits between STT and the user context aggregator. On each final transcript it
updates the rolling language estimate; when the caller's band changes, it
pushes an ``LLMMessagesTransformFrame`` that swaps the ``[LANG-STYLE]``
directive — pushed *before* the transcription frame is forwarded, so the new
directive is in context before the turn's LLM run.
"""

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    LLMMessagesAppendFrame,
    LLMMessagesTransformFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from .directives import replace_directive
from .ratio import hindi_ratio
from .tracker import LanguageBandTracker


def user_text_from_frame(frame: Frame) -> str | None:
    """User text carried by a frame, if any.

    Covers both entry paths for a user turn: STT ``TranscriptionFrame`` (voice)
    and ``LLMMessagesAppendFrame`` with a user message (RTVI send-text, which
    is how text-mode evals deliver turns).
    """
    if isinstance(frame, TranscriptionFrame):
        return frame.text
    if isinstance(frame, LLMMessagesAppendFrame):
        parts: list[str] = []
        for message in frame.messages:
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts.extend(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
        if parts:
            return " ".join(parts)
    return None


class LanguageMirrorProcessor(FrameProcessor):
    """Tracks the caller's language mix and steers the LLM to match."""

    def __init__(self, *, tracker: LanguageBandTracker | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._tracker = tracker or LanguageBandTracker()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        text = user_text_from_frame(frame)
        if text is not None:
            ratio = hindi_ratio(text)
            band = self._tracker.observe(ratio)
            logger.info(
                "lang ratio={} ema={:.2f} band={}{}",
                f"{ratio:.2f}" if ratio is not None else "n/a",
                self._tracker.ema,
                self._tracker.band,
                " (changed)" if band else "",
            )
            if band is not None:
                await self.push_frame(
                    LLMMessagesTransformFrame(transform=replace_directive(band)),
                    direction,
                )

        await self.push_frame(frame, direction)
