"""
Unit tests for Twilio Media Streams Telephony Protocol Models (src/voice/telephony_models.py).
"""

import base64
import sys
import unittest
from pathlib import Path

_VOICE_DIR = str(Path(__file__).resolve().parents[1] / "src" / "voice")
if _VOICE_DIR not in sys.path:
    sys.path.insert(0, _VOICE_DIR)

from telephony_models import (
    CallSession,
    CallStatus,
    TwilioEventType,
    TwilioMediaData,
    TwilioProtocolError,
    TwilioStartData,
    build_clear_message,
    build_mark_message,
    build_media_message,
    parse_twilio_frame,
)


class TestTelephonyModels(unittest.TestCase):
    def test_parse_connected_frame(self):
        raw = {"event": "connected", "protocol": "Call", "version": "1.0.0"}
        evt_type, data = parse_twilio_frame(raw)
        self.assertEqual(evt_type, TwilioEventType.CONNECTED)
        self.assertEqual(data["protocol"], "Call")

    def test_parse_start_frame_valid(self):
        raw = {
            "event": "start",
            "streamSid": "MZ123",
            "start": {
                "accountSid": "AC123",
                "streamSid": "MZ123",
                "callSid": "CA123",
                "tracks": ["inbound"],
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                "customParameters": {"userId": "patient_42"},
            },
        }
        evt_type, data = parse_twilio_frame(raw)
        self.assertEqual(evt_type, TwilioEventType.START)
        self.assertIsInstance(data, TwilioStartData)
        self.assertEqual(data.call_sid, "CA123")
        self.assertEqual(data.stream_sid, "MZ123")
        self.assertEqual(data.custom_parameters["userId"], "patient_42")

    def test_parse_start_frame_rejects_unsupported_codec(self):
        raw = {
            "event": "start",
            "start": {
                "accountSid": "AC123",
                "streamSid": "MZ123",
                "callSid": "CA123",
                "mediaFormat": {"encoding": "audio/pcm", "sampleRate": 16000, "channels": 1},
            },
        }
        with self.assertRaises(TwilioProtocolError) as ctx:
            parse_twilio_frame(raw)
        self.assertIn("Unsupported audio encoding", str(ctx.exception))

    def test_parse_media_frame_decodes_bytes(self):
        original_bytes = b"\xff\xaa\x55\x00" * 40
        b64_str = base64.b64encode(original_bytes).decode("ascii")
        raw = {
            "event": "media",
            "streamSid": "MZ123",
            "media": {"payload": b64_str, "timestamp": "12345", "chunk": "1"},
        }
        evt_type, data = parse_twilio_frame(raw)
        self.assertEqual(evt_type, TwilioEventType.MEDIA)
        self.assertIsInstance(data, TwilioMediaData)
        self.assertEqual(data.decode_raw_bytes(), original_bytes)

    def test_parse_stop_frame(self):
        raw = {
            "event": "stop",
            "streamSid": "MZ123",
            "stop": {"accountSid": "AC123", "callSid": "CA123"},
        }
        evt_type, data = parse_twilio_frame(raw)
        self.assertEqual(evt_type, TwilioEventType.STOP)
        self.assertEqual(data["call_sid"], "CA123")

    def test_build_clear_message(self):
        msg = build_clear_message("MZ123")
        self.assertEqual(msg, {"event": "clear", "streamSid": "MZ123"})

    def test_build_mark_message(self):
        msg = build_mark_message("MZ123", "greeting_done")
        self.assertEqual(msg, {"event": "mark", "streamSid": "MZ123", "mark": {"name": "greeting_done"}})

    def test_build_media_message(self):
        raw_bytes = b"\x01\x02\x03\x04"
        msg = build_media_message("MZ123", raw_bytes)
        self.assertEqual(msg["event"], "media")
        self.assertEqual(msg["streamSid"], "MZ123")
        decoded = base64.b64decode(msg["media"]["payload"])
        self.assertEqual(decoded, raw_bytes)

    def test_call_session_isolation(self):
        s1 = CallSession(call_sid="CA1", stream_sid="MZ1", session_id="sess_1")
        s2 = CallSession(call_sid="CA2", stream_sid="MZ2", session_id="sess_2")

        t1 = s1.next_turn()
        t2 = s1.next_turn()
        self.assertEqual(t1, 1)
        self.assertEqual(t2, 2)
        self.assertEqual(s2.turn_count, 0)  # s2 unaffected

        s1.interrupt_current_turn()
        self.assertTrue(s1.is_interrupted)
        self.assertEqual(s1.status, CallStatus.INTERRUPTED)
        self.assertFalse(s2.is_interrupted)


if __name__ == "__main__":
    unittest.main()
