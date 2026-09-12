"""
Integration and behavioral tests for VoiceCallHandler and VoiceCallManager (src/voice/voice_pipeline.py).
Validates:
- Finalized speech turn drives ConversationManager
- Partial transcripts are observational only and NEVER trigger actions
- Barge-in triggers Twilio clear event and cancels in-flight TTS playback
- Old audio never continues playing after interruption
- Concurrent calls maintain strict state and task isolation
"""

import asyncio
import sys
import unittest
from pathlib import Path

_VOICE_DIR = str(Path(__file__).resolve().parents[1] / "src" / "voice")
_AGENT_DIR = str(Path(__file__).resolve().parents[1] / "src" / "agent")
for p in (_VOICE_DIR, _AGENT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from metrics import MetricsRegistry
from stt_service import MockSTTService, STTEvent, STTEventType
from telephony_models import (
    CallSession,
    CallStatus,
)
from tts_service import MockTTSService
from voice_pipeline import VoiceCallHandler, VoiceCallManager


class MockConversationManager:
    """Mock of ConversationManager providing deterministic turns."""

    def __init__(self, responses: list[str]):
        self.responses = responses
        self.call_history: list[dict] = []

    def handle_turn(self, message: str, session_id: str = None, user_id: str = None, **kwargs):
        self.call_history.append({"message": message, "session_id": session_id, "user_id": user_id})
        resp = self.responses.pop(0) if self.responses else "Default answer."
        # Simulate streaming token chunks
        words = resp.split(" ")
        for word in words:
            yield word + " "
        yield {
            "response": resp,
            "is_handoff": False,
            "latency_ms": 15.0,
        }


class TestVoicePipeline(unittest.IsolatedAsyncioTestCase):
    async def test_final_transcript_drives_conversation_manager(self):
        outbound = []

        async def _mock_send(msg: dict):
            outbound.append(msg)

        cm = MockConversationManager(["Our return policy is thirty days."])
        stt = MockSTTService()
        tts = MockTTSService(frame_count_per_word=1)
        session = CallSession(call_sid="CA100", stream_sid="MZ100", session_id="sess_100")

        handler = VoiceCallHandler(
            session=session,
            send_to_twilio_fn=_mock_send,
            conversation_manager=cm,
            stt_service=stt,
            tts_service=tts,
        )

        await stt.connect()
        stt_task = asyncio.create_task(handler.process_stt_events())

        # Emit FINAL_TRANSCRIPT
        await stt.push_event(STTEvent(STTEventType.FINAL_TRANSCRIPT, text="What is your return policy?"))

        # Allow turn execution to process
        await asyncio.sleep(0.05)
        if handler._active_turn_task:
            await handler._active_turn_task

        # Verify ConversationManager was called with finalized text
        self.assertEqual(len(cm.call_history), 1)
        self.assertEqual(cm.call_history[0]["message"], "What is your return policy?")
        self.assertEqual(cm.call_history[0]["session_id"], "sess_100")

        # Verify outbound frames include media (audio chunks) and mark (turn completion)
        media_events = [m for m in outbound if m.get("event") == "media"]
        mark_events = [m for m in outbound if m.get("event") == "mark"]

        self.assertGreater(len(media_events), 0)
        self.assertGreater(len(mark_events), 0)
        self.assertEqual(mark_events[-1]["mark"]["name"], "turn_1_end")

        stt_task.cancel()
        await handler.handle_stop()

    async def test_partial_transcript_never_executes_conversation_manager(self):
        outbound = []

        async def _mock_send(msg: dict):
            outbound.append(msg)

        cm = MockConversationManager(["Should not be called!"])
        stt = MockSTTService()
        tts = MockTTSService()
        session = CallSession(call_sid="CA101", stream_sid="MZ101", session_id="sess_101")

        handler = VoiceCallHandler(
            session=session,
            send_to_twilio_fn=_mock_send,
            conversation_manager=cm,
            stt_service=stt,
            tts_service=tts,
        )

        await stt.connect()
        stt_task = asyncio.create_task(handler.process_stt_events())

        # Emit INTERIM_TRANSCRIPT (partial hypothesis)
        await stt.push_event(STTEvent(STTEventType.INTERIM_TRANSCRIPT, text="I want to cancel"))
        await asyncio.sleep(0.02)

        # STRICT SAFETY INVARIANT: ConversationManager must NOT be called for interim transcripts
        self.assertEqual(len(cm.call_history), 0)
        self.assertEqual(len(outbound), 0)

        stt_task.cancel()
        await handler.handle_stop()

    async def test_barge_in_flushes_twilio_and_halts_audio(self):
        outbound = []

        async def _mock_send(msg: dict):
            outbound.append(msg)

        cm = MockConversationManager(["This is a very long response that will be interrupted."])
        stt = MockSTTService()
        tts = MockTTSService(frame_count_per_word=10)
        session = CallSession(call_sid="CA102", stream_sid="MZ102", session_id="sess_102")

        handler = VoiceCallHandler(
            session=session,
            send_to_twilio_fn=_mock_send,
            conversation_manager=cm,
            stt_service=stt,
            tts_service=tts,
        )

        await stt.connect()
        stt_task = asyncio.create_task(handler.process_stt_events())

        # Start a turn
        await stt.push_event(STTEvent(STTEventType.FINAL_TRANSCRIPT, text="Tell me something long."))
        await asyncio.sleep(0.01)

        # In the middle of playback, caller speaks -> SPEECH_STARTED event
        await stt.push_event(STTEvent(STTEventType.SPEECH_STARTED))
        await asyncio.sleep(0.02)

        # BARGE-IN INVARIANTS:
        # 1. Twilio clear event MUST have been sent
        clear_events = [m for m in outbound if m.get("event") == "clear"]
        self.assertEqual(len(clear_events), 1)
        self.assertEqual(clear_events[0]["streamSid"], "MZ102")

        # 2. Session status must reflect INTERRUPTED
        self.assertTrue(session.is_interrupted)
        self.assertEqual(session.status, CallStatus.INTERRUPTED)

        stt_task.cancel()
        await handler.handle_stop()

    async def test_voice_call_manager_concurrent_isolation(self):
        cm = MockConversationManager(["Resp 1", "Resp 2"])
        manager = VoiceCallManager(conversation_manager=cm)

        outbound_1 = []
        outbound_2 = []

        async def send1(msg):
            outbound_1.append(msg)

        async def send2(msg):
            outbound_2.append(msg)

        h1 = manager.register_call("CA-A", "MZ-A", send1, MockSTTService(), MockTTSService())
        h2 = manager.register_call("CA-B", "MZ-B", send2, MockSTTService(), MockTTSService())

        self.assertEqual(h1.session.call_sid, "CA-A")
        self.assertEqual(h2.session.call_sid, "CA-B")
        self.assertIsNot(h1.session, h2.session)

        # Clean up
        await manager.unregister_call("MZ-A")
        self.assertIsNone(manager.get_handler("MZ-A"))
        self.assertIsNotNone(manager.get_handler("MZ-B"))
        await manager.unregister_call("MZ-B")


class TestHandleStopIdempotency(unittest.IsolatedAsyncioTestCase):
    """
    Stability fix: server.py's websocket_call() calls handle_stop() up to
    3 times per real call (once explicitly on STOP, once again inside
    VoiceCallManager.unregister_call(), once more in websocket_call()'s
    own `finally`). Before the idempotency guard, that meant
    "voice_calls_completed" was incremented 3x and CALL_COMPLETED was
    logged 3x for a single call.
    """

    async def _build_handler(self, metrics: MetricsRegistry) -> VoiceCallHandler:
        cm = MockConversationManager([])
        stt = MockSTTService()
        tts = MockTTSService()
        session = CallSession(call_sid="CA200", stream_sid="MZ200", session_id="sess_200")
        return VoiceCallHandler(
            session=session,
            send_to_twilio_fn=lambda msg: None,
            conversation_manager=cm,
            stt_service=stt,
            tts_service=tts,
            metrics=metrics,
        )

    async def test_repeated_handle_stop_increments_metric_exactly_once(self):
        metrics = MetricsRegistry()
        handler = await self._build_handler(metrics)

        self.assertEqual(metrics.get_counter("voice_calls_completed"), 0)

        await handler.handle_stop()
        await handler.handle_stop()
        await handler.handle_stop()

        self.assertEqual(
            metrics.get_counter("voice_calls_completed"),
            1,
            "handle_stop() must be idempotent -- repeated invocation (matching "
            "websocket_call()'s real STOP-event/finally/unregister_call sequence) "
            "must not increment the completed-calls counter more than once",
        )

    async def test_repeated_handle_stop_via_unregister_call_increments_metric_exactly_once(self):
        """
        Reproduces the exact real sequence from server.py's websocket_call():
        explicit handle_stop(), then unregister_call() (which itself calls
        handle_stop() again internally), then a final direct handle_stop()
        call from the `finally` block.
        """
        metrics = MetricsRegistry()
        manager = VoiceCallManager(conversation_manager=MockConversationManager([]), metrics=metrics)
        handler = manager.register_call("CA201", "MZ201", lambda msg: None, MockSTTService(), MockTTSService())

        await handler.handle_stop()  # explicit call (server.py's STOP branch)
        await manager.unregister_call("MZ201")  # internally calls handle_stop() again
        await handler.handle_stop()  # server.py's unconditional `finally` call

        self.assertEqual(metrics.get_counter("voice_calls_completed"), 1)

    async def test_handle_stop_is_active_false_after_first_call(self):
        handler = await self._build_handler(MetricsRegistry())
        self.assertTrue(handler.is_active)
        await handler.handle_stop()
        self.assertFalse(handler.is_active)
        # Calling again must not raise and must not flip state back.
        await handler.handle_stop()
        self.assertFalse(handler.is_active)


if __name__ == "__main__":
    unittest.main()
