"""JSONL conversation transcripts for evaluation and operational review."""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from language.processor import user_text_from_frame

_IST = ZoneInfo("Asia/Kolkata")
_ANIMALS_BY_WEEKDAY = {
    0: "Wolf",
    1: "Fox",
    2: "Otter",
    3: "Tiger",
    4: "Dolphin",
    5: "Bear",
    6: "Owl",
}


def transcript_filename(created_at: datetime) -> str:
    """Return the local-time filename shared by every session on a weekday."""
    local_time = created_at.astimezone(_IST)
    animal = _ANIMALS_BY_WEEKDAY[local_time.weekday()]
    return (
        f"{animal}_{local_time.hour}_{local_time.minute:02d}_"
        f"{local_time.day:02d}_{local_time.month:02d}_{local_time.year}.jsonl"
    )


class ConversationTranscript:
    """Append-only transcript writer; one JSON object per conversation event."""

    def __init__(self, directory: Path, session_id: str | None) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        candidate = directory / transcript_filename(datetime.now(_IST))
        suffix = 2
        while candidate.exists():
            candidate = directory / f"{candidate.stem}_{suffix}{candidate.suffix}"
            suffix += 1
        self.path = candidate
        self._write({"event": "session_started", "session_id": session_id})

    def log_turn(self, role: str, content: str) -> None:
        if content.strip():
            self._write({"event": "turn", "role": role, "content": content})

    def log_event(self, event: str, **data) -> None:
        self._write({'event': event, **data})

    def _write(self, payload: dict) -> None:
        payload["timestamp"] = datetime.now(_IST).isoformat()
        with self.path.open("a", encoding="utf-8") as transcript:
            transcript.write(json.dumps(payload, ensure_ascii=False) + "\n")


class ConversationTranscriptProcessor(FrameProcessor):
    """Records final user turns or complete LLM responses without changing frames."""

    def __init__(self, *, transcript: ConversationTranscript, role: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._transcript = transcript
        self._role = role
        self._assistant_parts: list[str] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if self._role == "user":
            text = user_text_from_frame(frame)
            if text is not None:
                self._transcript.log_turn("user", text)
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._assistant_parts.clear()
        elif isinstance(frame, LLMTextFrame):
            self._assistant_parts.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._transcript.log_turn("assistant", "".join(self._assistant_parts))
            self._assistant_parts.clear()

        await self.push_frame(frame, direction)
