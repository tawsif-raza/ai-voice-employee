"""
Integration tests for Telephony Voice Gateway API routes (src/api/server.py).
Tests:
- POST /twiml/inbound-call returns valid TwiML XML with <Connect><Stream>
- GET /twiml/inbound-call returns valid TwiML XML
- WebSocket /ws/call handles Twilio Media Streams protocol (start, media, stop)
"""

import base64
import json
import os
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
from fastapi.testclient import TestClient
from handoff_detector import HandoffDetector
from twilio_signature import compute_signature
from voice_pipeline import VoiceCallManager

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
        resp = self.client.get(
            "/twiml/inbound-call", headers={"Host": "api.testvoice.com", "X-Forwarded-Proto": "https"}
        )
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


class TestVoiceHealthEndpoint(unittest.TestCase):
    """
    Phase 1.4: /health/voice previously had no dedicated test coverage at
    all. Covers both the pre-existing provider fields and the new
    twilio_account_configured / twilio_signature_enforced fields added
    alongside the signature-enforcement work above.
    """

    def setUp(self):
        self.cm = _build_test_cm()
        server._conversation_manager = self.cm
        server._voice_call_manager = VoiceCallManager(conversation_manager=self.cm)
        self.client = TestClient(server.app)
        self._backup = {
            k: os.environ.get(k)
            for k in (
                "TWILIO_ACCOUNT_SID",
                "TWILIO_AUTH_TOKEN",
                "ANTHROPIC_API_KEY",
                "GEMINI_API_KEY",
                "GROQ_API_KEY",
                "LLM_PROVIDER",
                "VOICE_MOCK_SERVICES",
            )
        }
        for k in self._backup:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_no_credentials_configured(self):
        resp = self.client.get("/health/voice")
        self.assertEqual(resp.status_code, 200)
        providers = resp.json()["providers"]
        self.assertFalse(providers["twilio_account_configured"])
        self.assertFalse(providers["twilio_signature_enforced"])
        self.assertFalse(providers["claude_configured"])
        self.assertFalse(providers["gemini_configured"])
        self.assertFalse(providers["groq_configured"])
        # No credentials at all -> the free, fully-offline local model, not
        # a hardcoded display value (see llm_provider._default_provider_mode()).
        self.assertEqual(providers["llm_provider"], "local")

    def test_free_tier_credentials_resolve_to_free_fallback(self):
        """Gemini + Groq (both free) with no Claude key must resolve to
        free_fallback, never silently to the paid "fallback" mode."""
        os.environ["GEMINI_API_KEY"] = "fake-gemini-key-for-test"
        os.environ["GROQ_API_KEY"] = "fake-groq-key-for-test"
        resp = self.client.get("/health/voice")
        providers = resp.json()["providers"]
        self.assertTrue(providers["gemini_configured"])
        self.assertTrue(providers["groq_configured"])
        self.assertFalse(providers["claude_configured"])
        self.assertEqual(providers["llm_provider"], "free_fallback")

    def test_twilio_credentials_configured(self):
        os.environ["TWILIO_ACCOUNT_SID"] = "AC_fake_for_test"
        os.environ["TWILIO_AUTH_TOKEN"] = "fake_token_for_test"
        resp = self.client.get("/health/voice")
        providers = resp.json()["providers"]
        self.assertTrue(providers["twilio_account_configured"])
        self.assertTrue(providers["twilio_signature_enforced"])
        # Never echoes the actual configured value anywhere in the body.
        self.assertNotIn("fake_token_for_test", resp.text)

    def test_active_call_count_reflects_voice_manager(self):
        resp = self.client.get("/health/voice")
        self.assertEqual(resp.json()["active_call_count"], 0)


class TestTwilioWebhookSignatureValidation(unittest.TestCase):
    """
    Phase 1.4 (docs/phase1.4-external-integration-report.md Section 2/3):
    /twiml/inbound-call must enforce X-Twilio-Signature whenever
    TWILIO_AUTH_TOKEN is configured, and must never block a request when
    it isn't (the existing tests above, run with no TWILIO_AUTH_TOKEN set,
    already cover that unconfigured case passing through untouched).
    """

    def setUp(self):
        self.cm = _build_test_cm()
        server._conversation_manager = self.cm
        server._voice_call_manager = VoiceCallManager(conversation_manager=self.cm)
        self.client = TestClient(server.app)
        self._prior_token = os.environ.get("TWILIO_AUTH_TOKEN")
        os.environ["TWILIO_AUTH_TOKEN"] = "test-auth-token-not-real"

    def tearDown(self):
        if self._prior_token is None:
            os.environ.pop("TWILIO_AUTH_TOKEN", None)
        else:
            os.environ["TWILIO_AUTH_TOKEN"] = self._prior_token

    def test_valid_signature_is_accepted(self):
        params = {"CallSid": "CA123", "From": "+15551234567"}
        url = "http://api.testvoice.com/twiml/inbound-call"
        sig = compute_signature("test-auth-token-not-real", url, params)
        resp = self.client.post(
            "/twiml/inbound-call",
            data=params,
            headers={"Host": "api.testvoice.com", "X-Twilio-Signature": sig},
        )
        self.assertEqual(resp.status_code, 200)

    def test_missing_signature_is_rejected(self):
        resp = self.client.post("/twiml/inbound-call", data={"CallSid": "CA123"}, headers={"Host": "api.testvoice.com"})
        self.assertEqual(resp.status_code, 403)

    def test_wrong_signature_is_rejected(self):
        resp = self.client.post(
            "/twiml/inbound-call",
            data={"CallSid": "CA123"},
            headers={"Host": "api.testvoice.com", "X-Twilio-Signature": "not-the-real-signature"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_tampered_body_is_rejected(self):
        """Signature computed for one CallSid, request sent with a different one -- must not validate."""
        sig = compute_signature(
            "test-auth-token-not-real", "http://api.testvoice.com/twiml/inbound-call", {"CallSid": "CA_ORIGINAL"}
        )
        resp = self.client.post(
            "/twiml/inbound-call",
            data={"CallSid": "CA_TAMPERED"},
            headers={"Host": "api.testvoice.com", "X-Twilio-Signature": sig},
        )
        self.assertEqual(resp.status_code, 403)

    def test_no_token_configured_skips_validation(self):
        """Restores the existing (pre-Phase-1.4) unconfigured behavior -- covered again here explicitly."""
        os.environ.pop("TWILIO_AUTH_TOKEN", None)
        resp = self.client.post("/twiml/inbound-call", data={"CallSid": "CA123"}, headers={"Host": "api.testvoice.com"})
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
