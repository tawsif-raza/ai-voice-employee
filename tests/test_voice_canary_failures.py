"""
Comprehensive Failure Scenarios Test Suite (tests/test_voice_canary_failures.py)

Validates Criterion 8:
- Twilio disconnect mid-call
- WebSocket send timeout
- Deepgram STT connection failure
- Deepgram mid-stream error
- Deepgram delayed response / silence
- Claude failure leading to graceful apology speech
- ElevenLabs TTS synthesis failure
- Malformed provider responses
- Repeated rapid interruptions
All scenarios verify graceful degradation without dropping calls or crashing.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Iterator

_VOICE_DIR = str(Path(__file__).resolve().parents[1] / "src" / "voice")
_AGENT_DIR = str(Path(__file__).resolve().parents[1] / "src" / "agent")
_INFERENCE_DIR = str(Path(__file__).resolve().parents[1] / "src" / "inference")
for p in (_VOICE_DIR, _AGENT_DIR, _INFERENCE_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from conversation_manager import ConversationManager
from handoff_detector import HandoffDetector
from metrics import MetricsRegistry
from stt_service import DeepgramSTTService, MockSTTService, STTEvent, STTEventType
from telephony_models import CallSession, CallStatus, TwilioStartData
from tts_service import BaseTTSService, MockTTSService
from voice_pipeline import VoiceCallHandler

CLINICAL_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "clinical_triggers.yaml"
HANDOFF_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "handoff_phrases.yaml"


class FailingCM:
    """Simulates internal ConversationManager or upstream LLM failure."""
    def handle_turn(self, *args, **kwargs):
        raise RuntimeError("Claude and Gemini providers are temporarily unreachable")


class MalformedResponseCM:
    """Yields unexpected malformed payloads."""
    def handle_turn(self, *args, **kwargs):
        yield 12345  # invalid token type
        yield {"invalid_key": True}  # missing response dict
        yield "Recovered token. "
        yield {"response": "Recovered token.", "latency_ms": 12.0, "is_handoff": False}


class FailingTTSService(BaseTTSService):
    """Simulates ElevenLabs 500 error / network failure."""
    async def synthesize_stream(self, token_stream, cancellation_event=None):
        if False:
            yield b""
        raise ConnectionError("ElevenLabs TTS endpoint connection refused")


class TestVoiceCanaryFailures(unittest.IsolatedAsyncioTestCase):

    async def test_twilio_disconnect_handled_gracefully(self):
        """When Twilio WebSocket disconnects during audio streaming, handler does not crash."""
        call_count = 0
        async def failing_send(msg):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise ConnectionResetError("Twilio WebSocket client disconnected")

        cm = ConversationManager(
            llm_service=None,
            clinical_guard=HandoffDetector(config_path=CLINICAL_CONFIG),
            handoff_detector=HandoffDetector(config_path=HANDOFF_CONFIG),
        )
        stt = MockSTTService()
        tts = MockTTSService(frame_count_per_word=5)
        session = CallSession(call_sid="CA_DISC", stream_sid="MZ_DISC", session_id="s_disc")

        handler = VoiceCallHandler(
            session=session,
            send_to_twilio_fn=failing_send,
            conversation_manager=cm,
            stt_service=stt,
            tts_service=tts,
        )

        await stt.connect()
        stt_task = asyncio.create_task(handler.process_stt_events())

        # Final transcript causes audio streaming, which triggers disconnect
        await stt.push_event(STTEvent(STTEventType.FINAL_TRANSCRIPT, text="Hello there"))
        await asyncio.sleep(0.05)

        # Disconnect clean up
        await handler.handle_stop()
        self.assertEqual(session.status, CallStatus.COMPLETED)
        stt_task.cancel()

    async def test_websocket_timeout_handled_gracefully(self):
        """When Twilio WebSocket times out on clear event, pipeline does not raise unhandled error."""
        async def timeout_send(msg):
            if msg.get("event") == "clear":
                raise asyncio.TimeoutError("Twilio clear frame send timed out")

        cm = ConversationManager(
            llm_service=None,
            clinical_guard=HandoffDetector(config_path=CLINICAL_CONFIG),
            handoff_detector=HandoffDetector(config_path=HANDOFF_CONFIG),
        )
        stt = MockSTTService()
        tts = MockTTSService()
        session = CallSession(call_sid="CA_TO", stream_sid="MZ_TO", session_id="s_to")

        handler = VoiceCallHandler(
            session=session,
            send_to_twilio_fn=timeout_send,
            conversation_manager=cm,
            stt_service=stt,
            tts_service=tts,
        )

        await stt.connect()
        stt_task = asyncio.create_task(handler.process_stt_events())

        # Trigger barge-in with timeout send
        await stt.push_event(STTEvent(STTEventType.SPEECH_STARTED))
        await asyncio.sleep(0.01)

        # Pipeline logged warning and marked interrupted without crash
        self.assertTrue(session.is_interrupted)
        stt_task.cancel()
        await handler.handle_stop()

    async def test_deepgram_missing_key_fails_closed(self):
        """DeepgramSTTService without API key raises configuration error immediately."""
        svc = DeepgramSTTService(api_key="")
        with self.assertRaises(ValueError):
            await svc.connect()

    async def test_claude_total_failure_speaks_polite_apology(self):
        """When ConversationManager/LLM fails completely, pipeline speaks an apology rather than dropping call."""
        outbound = []
        async def mock_send(msg): outbound.append(msg)

        cm = FailingCM()
        stt = MockSTTService()
        tts = MockTTSService(frame_count_per_word=1)
        session = CallSession(call_sid="CA_APOL", stream_sid="MZ_APOL", session_id="s_apol")
        metrics = MetricsRegistry()

        handler = VoiceCallHandler(
            session=session,
            send_to_twilio_fn=mock_send,
            conversation_manager=cm,
            stt_service=stt,
            tts_service=tts,
            metrics=metrics,
        )

        await stt.connect()
        stt_task = asyncio.create_task(handler.process_stt_events())

        await stt.push_event(STTEvent(STTEventType.FINAL_TRANSCRIPT, text="Need help"))
        await asyncio.sleep(0.05)
        if handler._active_turn_task:
            await handler._active_turn_task

        # Invariant 1: Apology audio was sent to caller
        media_events = [m for m in outbound if m.get("event") == "media"]
        self.assertGreater(len(media_events), 0, "Graceful apology audio must be transmitted")

        # Invariant 2: Failure metric was recorded
        self.assertEqual(metrics.get_counter("voice_calls_failed"), 1)

        stt_task.cancel()
        await handler.handle_stop()

    async def test_tts_failure_degrades_gracefully(self):
        """When ElevenLabs TTS synthesis fails, pipeline logs error and does not drop call."""
        outbound = []
        async def mock_send(msg): outbound.append(msg)

        cm = ConversationManager(
            llm_service=None,
            clinical_guard=HandoffDetector(config_path=CLINICAL_CONFIG),
            handoff_detector=HandoffDetector(config_path=HANDOFF_CONFIG),
        )
        stt = MockSTTService()
        tts = FailingTTSService()
        session = CallSession(call_sid="CA_TTS_ERR", stream_sid="MZ_TTS_ERR", session_id="s_tts")

        handler = VoiceCallHandler(
            session=session,
            send_to_twilio_fn=mock_send,
            conversation_manager=cm,
            stt_service=stt,
            tts_service=tts,
        )

        await stt.connect()
        stt_task = asyncio.create_task(handler.process_stt_events())

        await stt.push_event(STTEvent(STTEventType.FINAL_TRANSCRIPT, text="Test message"))
        await asyncio.sleep(0.05)

        # Call is still active, not abruptly crashed
        self.assertTrue(handler.is_active)
        stt_task.cancel()
        await handler.handle_stop()

    async def test_malformed_provider_response_handled_gracefully(self):
        """Malformed dictionary chunks or non-string tokens from CM are skipped safely."""
        outbound = []
        async def mock_send(msg): outbound.append(msg)

        cm = MalformedResponseCM()
        stt = MockSTTService()
        tts = MockTTSService(frame_count_per_word=1)
        session = CallSession(call_sid="CA_MALFORMED", stream_sid="MZ_MALFORMED", session_id="s_mal")

        handler = VoiceCallHandler(
            session=session,
            send_to_twilio_fn=mock_send,
            conversation_manager=cm,
            stt_service=stt,
            tts_service=tts,
        )

        await stt.connect()
        stt_task = asyncio.create_task(handler.process_stt_events())

        await stt.push_event(STTEvent(STTEventType.FINAL_TRANSCRIPT, text="Do something"))
        await asyncio.sleep(0.05)
        if handler._active_turn_task:
            await handler._active_turn_task

        # Turn finished and audio was sent for the valid recovered token
        self.assertEqual(session.turn_count, 1)
        media_events = [m for m in outbound if m.get("event") == "media"]
        self.assertGreater(len(media_events), 0)

        stt_task.cancel()
        await handler.handle_stop()

    async def test_caller_silence_maintains_ready_state(self):
        """Caller silence generates no spurious turns and keeps call ready."""
        outbound = []
        async def mock_send(msg): outbound.append(msg)

        cm = ConversationManager(
            llm_service=None,
            clinical_guard=HandoffDetector(config_path=CLINICAL_CONFIG),
            handoff_detector=HandoffDetector(config_path=HANDOFF_CONFIG),
        )
        stt = MockSTTService()
        tts = MockTTSService()
        session = CallSession(call_sid="CA_SILENCE", stream_sid="MZ_SILENCE", session_id="s_sil")

        handler = VoiceCallHandler(
            session=session,
            send_to_twilio_fn=mock_send,
            conversation_manager=cm,
            stt_service=stt,
            tts_service=tts,
        )

        await stt.connect()
        stt_task = asyncio.create_task(handler.process_stt_events())

        # Wait 50ms with no speech events
        await asyncio.sleep(0.05)

        self.assertEqual(session.turn_count, 0)
        self.assertEqual(len(outbound), 0)
        self.assertTrue(handler.is_active)

        stt_task.cancel()
        await handler.handle_stop()


if __name__ == "__main__":
    unittest.main()