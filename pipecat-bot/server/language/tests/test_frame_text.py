"""user_text_from_frame: both user-turn entry paths (STT and RTVI send-text)."""

from pipecat.frames.frames import LLMMessagesAppendFrame, TranscriptionFrame

from language.processor import user_text_from_frame


def test_transcription_frame_text():
    frame = TranscriptionFrame(text="mera order kahan hai", user_id="u", timestamp="t")
    assert user_text_from_frame(frame) == "mera order kahan hai"


def test_append_frame_user_message():
    frame = LLMMessagesAppendFrame(messages=[{"role": "user", "content": "refund kab milega"}])
    assert user_text_from_frame(frame) == "refund kab milega"


def test_append_frame_ignores_non_user_roles():
    frame = LLMMessagesAppendFrame(
        messages=[
            {"role": "developer", "content": "do something"},
            {"role": "assistant", "content": "ok"},
        ]
    )
    assert user_text_from_frame(frame) is None


def test_append_frame_structured_content():
    frame = LLMMessagesAppendFrame(
        messages=[{"role": "user", "content": [{"type": "text", "text": "gift card return"}]}]
    )
    assert user_text_from_frame(frame) == "gift card return"


def test_unrelated_frame_is_none():
    class Other:
        pass

    assert user_text_from_frame(Other()) is None
