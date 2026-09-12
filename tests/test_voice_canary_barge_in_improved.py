"""
Improved Barge-In and Interruption Test Suite (tests/test_voice_canary_barge_in_improved.py)

Validates Criterion 7:
- Mid-sentence caller interruption halts in-flight TTS immediately
- Twilio `clear` event is dispatched to purge phone speaker queue
- Interrupted turn text is recorded in session history with [Interrupted by caller...] marker
- Rapid double-speech detection does not send duplicate clear events
- New user speech successfully resets turn cancellation token and starts turn #2
- Pending confirmation state is invalidated on barge-in to avoid accidental tool execution
"""

import asyncio
import sys
import unittest
from pathlib import Path

_VOICE_DIR = str(Path(__file__).resolve().parents[1] / "src" / "voice")
_AGENT_DIR = str(Path(__file__).resolve().parents[1] / "src" / "agent")
_INFERENCE_DIR = str(Path(__file__).resolve().parents[1] / "src" / "inference")
for p in (_VOICE_DIR, _AGENT_DIR, _INFERENCE_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from conversation_manager import ConversationManager
from handoff_detector import HandoffDetector
from metrics import MetricsRegistry
from session_manager import SessionManager
from session_models import SessionStatus
from stt_service import MockSTTService, STTEvent, STTEventType
from telephony_models import CallSession, CallStatus, TwilioStartData
from tts_service import MockTTSService
from voice_pipeline import VoiceCallHandler

CLINICAL_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "clinical_triggers.yaml"
HANDOFF_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "handoff_phrases.yaml"


class LongSpeechLLMService:
    def __init__(self, responses: list[str] = None):
        self.responses = responses or [
            "We have a comprehensive list of services including urgent care, primary care, dental checkups, and diagnostic labs.",
            "Certainly, let me help you update your residential mailing address right away.",
        ]

    def generate_stream(self, messages: list[dict], **kwargs):
        resp = self.responses.pop(0) if self.responses else "Default response."
        for token in resp.split():
            yield token + " "
        yield {"text": resp, "latency_ms": 15.0}


class TestVoiceCanaryBargeIn(unittest.IsolatedAsyncioTestCase):

    async def test_mid_sentence_barge_in_and_recovery(self):
        """
        Caller interrupts long utterance -> audio stops -> caller speaks new turn ->
        turn #2 completes cleanly without repeating stale content.
        """
        outbound = []
        async def mock_send(msg): outbound.append(msg)

        metrics = MetricsRegistry()
        cm = ConversationManager(
            llm_service=LongSpeechLLMService(),
            clinical_guard=HandoffDetector(config_path=CLINICAL_CONFIG),
            handoff_detector=HandoffDetector(config_path=HANDOFF_CONFIG),
            metrics=metrics,
        )

        stt = MockSTTService()
        # High frame count per word so synthesis takes long enough to interrupt
        tts = MockTTSService(frame_count_per_word=15)
        session = CallSession(call_sid="CA_BARGE_1", stream_sid="MZ_BARGE_1", session_id="sess_barge_1")

        handler = VoiceCallHandler(
            session=session,
            send_to_twilio_fn=mock_send,
            conversation_manager=cm,
            stt_service=stt,
            tts_service=tts,
            metrics=metrics,
        )

        start_data = TwilioStartData(
            account_sid="AC_TEST", stream_sid="MZ_BARGE_1", call_sid="CA_BARGE_1",
            tracks=["inbound"], media_format={"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}
        )
        await handler.handle_start(start_data)
        stt_task = asyncio.create_task(handler.process_stt_events())

        # 1. Turn 1 starts: long inquiry
        await stt.push_event(STTEvent(STTEventType.FINAL_TRANSCRIPT, text="Tell me about all your services."))
        # Wait until first audio is generated
        await asyncio.sleep(0.02)

        # 2. Caller interrupts: SPEECH_STARTED
        await stt.push_event(STTEvent(STTEventType.SPEECH_STARTED))
        await asyncio.sleep(0.01)

        # Invariant 1: Twilio clear event was transmitted
        clear_events = [m for m in outbound if m.get("event") == "clear"]
        self.assertEqual(len(clear_events), 1)

        # Invariant 2: Session marked interrupted
        self.assertTrue(session.is_interrupted)
        self.assertEqual(session.status, CallStatus.INTERRUPTED)

        # Invariant 3: Rapid second SPEECH_STARTED does NOT emit duplicate clear
        await stt.push_event(STTEvent(STTEventType.SPEECH_STARTED))
        await asyncio.sleep(0.005)
        clear_events_after = [m for m in outbound if m.get("event") == "clear"]
        self.assertEqual(len(clear_events_after), 1, "Must not send duplicate clear on rapid VAD")

        # Invariant 4: Turn 1 recorded in history with interruption note
        self.assertGreater(len(session.interrupted_turns), 0)
        self.assertEqual(session.interrupted_turns[0]["turn_id"], 1)

        # 3. Caller speaks new turn #2
        outbound.clear()
        await stt.push_event(STTEvent(STTEventType.FINAL_TRANSCRIPT, text="Actually, change my address please."))

        await asyncio.sleep(0.05)
        if handler._active_turn_task:
            await handler._active_turn_task

        # Invariant 5: Turn 2 executes cleanly as active turn #2
        self.assertEqual(session.turn_count, 2)
        self.assertFalse(session.is_interrupted)

        # New audio sent for turn #2
        new_media = [m for m in outbound if m.get("event") == "media"]
        self.assertGreater(len(new_media), 0)

        # Session history reflects both turns cleanly
        self.assertEqual(session.conversation_history[0]["content"], "Tell me about all your services.")
        self.assertIn("[Interrupted", session.conversation_history[1]["content"])
        self.assertEqual(session.conversation_history[2]["content"], "Actually, change my address please.")
        self.assertIn("update your residential", session.conversation_history[3]["content"])

        stt_task.cancel()
        await handler.handle_stop()

    async def test_pending_confirmation_invalidated_on_barge_in(self):
        """
        When a turn awaits user confirmation and the user interrupts,
        the pending confirmation workflow state must be safely cleared.
        """
        outbound = []
        async def mock_send(msg): outbound.append(msg)

        sm = SessionManager()
        sess_state = sm.create_session(session_id="caller_barge_conf", user_id="telephony_caller")
        sm.update_session(
            sess_state.session_id,
            workflow_state="AWAITING_CONFIRMATION",
            pending_action="CANCEL_APPOINTMENT",
            pending_parameters={"appointment_id": "appt_123"},
        )

        cm = ConversationManager(
            llm_service=LongSpeechLLMService(),
            clinical_guard=HandoffDetector(config_path=CLINICAL_CONFIG),
            handoff_detector=HandoffDetector(config_path=HANDOFF_CONFIG),
            session_manager=sm,
        )

        stt = MockSTTService()
        tts = MockTTSService(frame_count_per_word=10)
        session = CallSession(
            call_sid="CA_CONF",
            stream_sid="MZ_CONF",
            session_id=sess_state.session_id,
        )

        handler = VoiceCallHandler(
            session=session,
            send_to_twilio_fn=mock_send,
            conversation_manager=cm,
            stt_service=stt,
            tts_service=tts,
        )

        await stt.connect()
        stt_task = asyncio.create_task(handler.process_stt_events())
        await stt.push_event(STTEvent(STTEventType.FINAL_TRANSCRIPT, text="Cancel my appointment appt_123"))
        await asyncio.sleep(0.01)

        # Trigger barge-in
        await stt.push_event(STTEvent(STTEventType.SPEECH_STARTED))
        await asyncio.sleep(0.01)

        # Pending action must be invalidated so subsequent input does not accidentally execute it
        updated_state = sm.get_session(sess_state.session_id)
        self.assertIsNone(updated_state.workflow_state)
        self.assertIsNone(updated_state.pending_action)

        stt_task.cancel()
        await handler.handle_stop()


if __name__ == "__main__":
    unittest.main()