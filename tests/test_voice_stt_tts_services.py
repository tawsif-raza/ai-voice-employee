"""
Unit tests for STT and TTS streaming services (src/voice/stt_service.py, src/voice/tts_service.py).
"""

import asyncio
import sys
import unittest
from pathlib import Path

_VOICE_DIR = str(Path(__file__).resolve().parents[1] / "src" / "voice")
if _VOICE_DIR not in sys.path:
    sys.path.insert(0, _VOICE_DIR)

from stt_service import MockSTTService, STTEvent, STTEventType
from tts_service import MockTTSService, strip_wav_header


class TestSTTTTSServices(unittest.IsolatedAsyncioTestCase):

    def test_strip_wav_header_removes_riff_header(self):
        # 44-byte standard RIFF header + dummy audio
        riff_header = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVEfmt " + b"\x00" * 28
        self.assertEqual(len(riff_header), 44)
        audio_content = b"\xff" * 160
        wav_payload = riff_header + audio_content

        stripped = strip_wav_header(wav_payload)
        self.assertEqual(stripped, audio_content)

    def test_strip_wav_header_leaves_raw_mulaw_untouched(self):
        raw_mulaw = b"\x7f" * 160
        stripped = strip_wav_header(raw_mulaw)
        self.assertEqual(stripped, raw_mulaw)

    async def test_mock_stt_service_event_pipeline(self):
        stt = MockSTTService()
        await stt.connect()

        # Send audio frames
        await stt.send_audio(b"\xff" * 160)
        await stt.send_audio(b"\xff" * 160)
        self.assertEqual(len(stt.received_audio_bytes), 320)

        # Queue events: SpeechStarted, Interim, Final
        await stt.push_event(STTEvent(STTEventType.SPEECH_STARTED))
        await stt.push_event(STTEvent(STTEventType.INTERIM_TRANSCRIPT, text="book an"))
        await stt.push_event(STTEvent(STTEventType.FINAL_TRANSCRIPT, text="book an appointment"))
        await stt.close()

        received = []
        async for evt in stt.receive_events():
            received.append(evt)

        self.assertEqual(len(received), 3)
        self.assertEqual(received[0].event_type, STTEventType.SPEECH_STARTED)
        self.assertEqual(received[1].event_type, STTEventType.INTERIM_TRANSCRIPT)
        self.assertEqual(received[1].text, "book an")
        self.assertEqual(received[2].event_type, STTEventType.FINAL_TRANSCRIPT)
        self.assertEqual(received[2].text, "book an appointment")

    async def test_mock_tts_service_cancellation_on_barge_in(self):
        tts = MockTTSService(frame_count_per_word=10)
        cancel_evt = asyncio.Event()

        async def _token_generator():
            yield "Hello"
            yield "world"
            yield "this"
            yield "should"
            yield "be"
            yield "interrupted"

        frames = []
        tts_gen = tts.synthesize_stream(_token_generator(), cancellation_event=cancel_evt)

        async for frame in tts_gen:
            frames.append(frame)
            if len(frames) >= 5:
                # Trigger barge-in cancellation!
                cancel_evt.set()

        # Synthesis must have halted prematurely due to cancellation
        self.assertTrue(cancel_evt.is_set())
        self.assertLess(len(frames), 60)


if __name__ == "__main__":
    unittest.main()
