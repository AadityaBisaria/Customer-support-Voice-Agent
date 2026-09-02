import json

from pipecat.services.sarvam.tts import SarvamTTSService


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


async def test_sarvam_ws_config_matches_strict_schema():
    service = SarvamTTSService(
        api_key="test-key",
        sample_rate=8000,
        settings=SarvamTTSService.Settings(
            model="bulbul:v3",
            voice="aditya",
            language="hi-IN",
            min_buffer_size=20,
            pace=1.0,
        ),
    )
    service._websocket = FakeWebSocket()

    await service._send_config()

    message = json.loads(service._websocket.sent[-1])
    assert message["type"] == "config"
    assert message["data"] == {
        "language_code": "hi-IN",
        "speaker": "aditya",
        "output_audio_codec": "linear16",
        "output_audio_bitrate": "128k",
        "min_buffer_size": 20,
        "max_chunk_length": 150,
    }
    assert "model" not in message["data"]
    assert "speech_sample_rate" not in message["data"]


def test_sarvam_audio_payload_is_16bit_aligned():
    service = SarvamTTSService(
        api_key="test-key",
        sample_rate=16000,
        settings=SarvamTTSService.Settings(
            model="bulbul:v3",
            voice="aditya",
            language="hi-IN",
            min_buffer_size=20,
        ),
    )

    odd_pcm = b"\x01\x02\x03"
    assert service._normalize_audio_payload(odd_pcm) == b"\x01\x02"
    assert service._normalize_audio_payload(b"\x04\x05") == b"\x04\x05"

    wav = (
        b"RIFF"
        + (40).to_bytes(4, byteorder="little")
        + b"WAVE"
        + b"fmt "
        + (16).to_bytes(4, byteorder="little")
        + (1).to_bytes(2, byteorder="little")
        + (1).to_bytes(2, byteorder="little")
        + (16000).to_bytes(4, byteorder="little")
        + (16000).to_bytes(4, byteorder="little")
        + (2).to_bytes(2, byteorder="little")
        + (2).to_bytes(2, byteorder="little")
        + b"data"
        + (4).to_bytes(4, byteorder="little")
        + b"\x00\x01\x02\x03"
    )
    assert service._normalize_audio_payload(wav) == b"\x00\x01\x02\x03"
