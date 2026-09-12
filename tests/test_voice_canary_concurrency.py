"""
Concurrent Call Isolation Test Suite (tests/test_voice_canary_concurrency.py)

Validates Criterion 6:
- Multiple concurrent telephony sessions run simultaneously
- Strict session, conversation, tool, and audio stream isolation
- Distinct call_sid, stream_sid, and session_id across callers
- No state leakage between callers (e.g. barge-in on one call does not impact others)
- Clean teardown and resource deallocation on call disconnect
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
from stt_service import MockSTTService, STTEvent, STTEventType
from telephony_models import CallStatus, TwilioStartData
from tts_service import MockTTSService
from voice_pipeline import VoiceCallManager

CLINICAL_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "clinical_triggers.yaml"
HANDOFF_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "handoff_phrases.yaml"


class EchoLLMService:
    """Echoes back caller ID to verify strict prompt and response isolation."""
    def generate_stream(self, messages: list[dict], **kwargs):
        user_msg = messages[-1]["content"] if messages else ""
        resp = f"Processed for {user_msg}"
        for token in resp.split():
            yield token + " "
        yield {"text": resp, "latency_ms": 15.0}


class TestVoiceCanaryConcurrency(unittest.IsolatedAsyncioTestCase):

    async def test_concurrent_call_isolation(self):
        """
        Spin up 5 concurrent telephone calls, stream unique queries simultaneously,
        and verify complete isolation of conversation history and audio streams.
        """
        metrics = MetricsRegistry()
        cm = ConversationManager(
            llm_service=EchoLLMService(),
            clinical_guard=HandoffDetector(config_path=CLINICAL_CONFIG),
            handoff_detector=HandoffDetector(config_path=HANDOFF_CONFIG),
            metrics=metrics,
        )
        manager = VoiceCallManager(conversation_manager=cm, metrics=metrics)

        num_calls = 5
        outbound_queues = {i: [] for i in range(num_calls)}
        stt_services = {}
        handlers = {}

        # 1. Register 5 concurrent calls
        for i in range(num_calls):
            call_sid = f"CA_CONC_{i}"
            stream_sid = f"MZ_CONC_{i}"

            def make_sender(idx):
                async def _send(msg):
                    outbound_queues[idx].append(msg)
                return _send

            stt = MockSTTService()
            tts = MockTTSService(frame_count_per_word=1)
            stt_services[i] = stt

            handler = manager.register_call(
                call_sid=call_sid,
                stream_sid=stream_sid,
                send_fn=make_sender(i),
                stt_service=stt,
                tts_service=tts,
                custom_params={"sessionId": f"sess_conc_{i}", "userId": f"caller_{i}"},
            )
            handlers[i] = handler

            # Simulate Twilio START frame
            start_data = TwilioStartData(
                account_sid="AC_MOCK",
                stream_sid=stream_sid,
                call_sid=call_sid,
                tracks=["inbound"],
                media_format={"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                custom_parameters={"sessionId": f"sess_conc_{i}", "userId": f"caller_{i}"},
            )
            await handler.handle_start(start_data)

        self.assertEqual(len(manager._active_calls), num_calls)
        self.assertEqual(metrics.get_counter("voice_calls_total"), num_calls)

        # 2. Launch STT processing loops for all calls
        stt_tasks = [
            asyncio.create_task(handlers[i].process_stt_events())
            for i in range(num_calls)
        ]

        # 3. Simultaneously push distinct speech turns to each caller
        for i in range(num_calls):
            query = f"Account inquiry from caller #{i}"
            await stt_services[i].push_event(STTEvent(STTEventType.FINAL_TRANSCRIPT, text=query))

        # Allow all turns to process concurrently
        await asyncio.sleep(0.1)
        for i in range(num_calls):
            if handlers[i]._active_turn_task:
                await handlers[i]._active_turn_task

        # 4. Verify each call received ONLY its own audio and updated its own history
        for i in range(num_calls):
            session = handlers[i].session
            self.assertEqual(session.turn_count, 1)
            self.assertEqual(session.session_id, f"sess_conc_{i}")
            self.assertEqual(session.user_id, f"caller_{i}")
            self.assertIn(f"caller #{i}", session.conversation_history[0]["content"])
            self.assertIn(f"caller #{i}", session.conversation_history[1]["content"])

            # Verify audio frames arrived in the right outbound queue
            media = [m for m in outbound_queues[i] if m.get("event") == "media"]
            self.assertGreater(len(media), 0)
            for m in media:
                self.assertEqual(m["streamSid"], f"MZ_CONC_{i}")

        # 5. Verify barge-in on Call #0 does NOT affect Call #1
        await stt_services[0].push_event(STTEvent(STTEventType.SPEECH_STARTED))
        await asyncio.sleep(0.01)

        self.assertTrue(handlers[0].session.is_interrupted)
        self.assertFalse(handlers[1].session.is_interrupted)
        self.assertEqual(handlers[1].session.status, CallStatus.STREAMING)

        # 6. Teardown all calls
        for t in stt_tasks:
            t.cancel()

        for i in range(num_calls):
            await manager.unregister_call(f"MZ_CONC_{i}")

        self.assertEqual(len(manager._active_calls), 0)
        self.assertEqual(metrics.get_counter("voice_calls_completed"), num_calls)


if __name__ == "__main__":
    unittest.main()