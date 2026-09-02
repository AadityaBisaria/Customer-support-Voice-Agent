import asyncio, os, base64
from dotenv import load_dotenv
from pipecat.services.sarvam.tts import SarvamTTSService

load_dotenv()

async def main():
    s = SarvamTTSService(
        api_key=os.getenv('SARVAM_API_KEY'),
        sample_rate=8000,
        settings=SarvamTTSService.Settings(
            model=os.getenv('SARVAM_TTS_MODEL', 'bulbul:v3'),
            voice=os.getenv('SARVAM_TTS_VOICE', 'aditya'),
            language='hi-IN',
            min_buffer_size=20,
        ),
    )
    await s._connect()
    await s._send_text('hello this is a test')
    for i in range(5):
        msg = await s._websocket.recv()
        print('MESSAGE', i)
        print(type(msg))
        if isinstance(msg, str):
            print(msg[:400])
        else:
            print(msg)
        if isinstance(msg, dict):
            data = msg.get('data', {}) if isinstance(msg.get('data', {}), dict) else {}
            audio = data.get('audio')
            if audio:
                raw = base64.b64decode(audio)
                print('RAW_LEN', len(raw))
                print('RAW_HEAD', raw[:30])
                print('CONTENT_TYPE', data.get('content_type'))
    await s._disconnect()

asyncio.run(main())
