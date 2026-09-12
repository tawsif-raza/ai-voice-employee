"""
Integration tests for Telephony Voice Gateway API routes (src/api/server.py).
Tests:
- POST /twiml/inbound-call returns valid TwiML XML with <Connect><Stream>
- GET /twiml/inbound-call returns valid TwiML XML
- WebSocket /ws/call handles Twilio Media Streams protocol (start, media, stop)
"""

import base64
import json
import sys
import unittest
from pathlib import Path

_SRC_API = str(Path(__file__).resolve().parents[1] / "src" / "api")
_SRC_AGENT = str(Path(__file__).resolve().parents[1] / "src" / "agent")
_SRC_INFERENCE = str(Path(__file__).resolve().parents[1] / "src" / "inference")
_SRC_VOICE = str(Path(__file__).resolve().parents[1] / "src" / "voice")

for p in (_SRC_API, _SRC_AGENT, _SRC_INFERENCE, _SRC_VOICE):
    if p not in sys.path:
        sys.path.insert(0, p)

import server
from conversation_manager import ConversationManager
from handoff_detector import HandoffDetector
from voice_pipeline import VoiceCallManager

from fastapi.testclient import TestClient

CLINICAL_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "clinical_triggers.yaml"
HANDOFF_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "handoff_phrases.yaml"


class FakeLLMService:
    def __init__(self, response_text: str = "Our clinic is open Monday to Friday."):
        self.response_text = response_text

    def generate_stream(self, messages, **kwargs):
        for word in self.response_text.split(" "):
            yield word + " "
        yield {"text": self.response_text, "latency_ms": 10.0}


def _build_test_cm() -> ConversationManager:
    return ConversationManager(
        llm_service=FakeLLMService(),
        retriever=None,
        clinical_guard=HandoffDetector(config_path=CLINICAL_CONFIG_PATH),
        handoff_detector=HandoffDetector(config_path=HANDOFF_CONFIG_PATH),
    )


class TestVoiceServerIntegration(unittest.TestCase):

    def setUp(self):
        self.cm = _build_test_cm()
        server._conversation_manager = self.cm
        server._voice_call_manager = VoiceCallManager(conversation_manager=self.cm)
        self.client = TestClient(server.app)

    def test_twiml_inbound_call_post(self):
        resp = self.client.post("/twiml/inbound-call", headers={"Host": "api.testvoice.com"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/xml", resp.headers["content-type"])
        body = resp.text
        self.assertIn("<Response>", body)
        self.assertIn("<Connect>", body)
        self.assertIn('<Stream url="ws://api.testvoice.com/ws/call">', body)

    def test_twiml_inbound_call_get(self):
        resp = self.client.get("/twiml/inbound-call", headers={"Host": "api.testvoice.com", "X-Forwarded-Proto": "https"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('<Stream url="wss://api.testvoice.com/ws/call">', resp.text)

    def test_websocket_call_lifecycle(self):
        # Enable mock services so external network is not touched
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"VOICE_MOCK_SERVICES": "true"}):
            with self.client.websocket_connect("/ws/call") as ws:
                # 1. Twilio sends 'connected'
                ws.send_text(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))

                # 2. Twilio sends 'start'
                start_frame = {
                    "event": "start",
                    "streamSid": "MZ_TEST_1",
                    "start": {
                        "accountSid": "AC_TEST",
                        "streamSid": "MZ_TEST_1",
                        "callSid": "CA_TEST_1",
                        "tracks": ["inbound"],
                        "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                        "customParameters": {"userId": "patient_101"},
                    },
                }
                ws.send_text(json.dumps(start_frame))

                # Allow async server loop to process the start frame
                import time
                handler = None
                for _ in range(50):
                    handler = server._voice_call_manager.get_handler("MZ_TEST_1")
                    if handler is not None:
                        break
                    time.sleep(0.01)

                self.assertIsNotNone(handler)
                self.assertEqual(handler.session.call_sid, "CA_TEST_1")

                # 3. Twilio sends 'media'
                dummy_audio = base64.b64encode(b"\xff" * 160).decode("ascii")
                media_frame = {
                    "event": "media",
                    "streamSid": "MZ_TEST_1",
                    "media": {"payload": dummy_audio, "timestamp": "100", "chunk": "1"},
                }
                ws.send_text(json.dumps(media_frame))

                # 4. Twilio sends 'stop'
                stop_frame = {
                    "event": "stop",
                    "streamSid": "MZ_TEST_1",
                    "stop": {"accountSid": "AC_TEST", "callSid": "CA_TEST_1"},
                }
                ws.send_text(json.dumps(stop_frame))

            # After disconnect, handler is cleanly unregistered
            self.assertIsNone(server._voice_call_manager.get_handler("MZ_TEST_1"))


if __name__ == "__main__":
    unittest.main()
