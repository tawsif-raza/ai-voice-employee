"""
Unit tests for src/api/twilio_signature.py (Phase 1.4 external integration
closure). Fully offline, stdlib only -- validates the HMAC-SHA1 algorithm
itself, never a real Twilio request.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "api"))
from twilio_signature import compute_signature, is_validation_configured, validate_signature  # noqa: E402


class TestComputeSignature(unittest.TestCase):
    def test_deterministic_for_same_inputs(self):
        sig1 = compute_signature("token123", "https://example.com/twiml/inbound-call", {"CallSid": "CA123"})
        sig2 = compute_signature("token123", "https://example.com/twiml/inbound-call", {"CallSid": "CA123"})
        self.assertEqual(sig1, sig2)

    def test_param_order_does_not_affect_signature(self):
        """Twilio sorts params by key internally -- caller-supplied dict order must not matter."""
        sig_a = compute_signature("token123", "https://example.com/x", {"A": "1", "B": "2"})
        sig_b = compute_signature("token123", "https://example.com/x", {"B": "2", "A": "1"})
        self.assertEqual(sig_a, sig_b)

    def test_different_url_produces_different_signature(self):
        sig_a = compute_signature("token123", "https://example.com/a", {})
        sig_b = compute_signature("token123", "https://example.com/b", {})
        self.assertNotEqual(sig_a, sig_b)

    def test_different_token_produces_different_signature(self):
        sig_a = compute_signature("token-a", "https://example.com/x", {"K": "v"})
        sig_b = compute_signature("token-b", "https://example.com/x", {"K": "v"})
        self.assertNotEqual(sig_a, sig_b)

    def test_no_params_signs_url_alone(self):
        """The WebSocket-upgrade case (no POST body) -- must not crash on empty params."""
        sig = compute_signature("token123", "wss://example.com/ws/call", {})
        self.assertIsInstance(sig, str)
        self.assertGreater(len(sig), 0)


class TestValidateSignature(unittest.TestCase):
    def test_correct_signature_validates(self):
        url = "https://example.com/twiml/inbound-call"
        params = {"CallSid": "CA123", "From": "+15551234567"}
        sig = compute_signature("secret-token", url, params)
        self.assertTrue(validate_signature("secret-token", url, params, sig))

    def test_tampered_param_fails_validation(self):
        url = "https://example.com/twiml/inbound-call"
        sig = compute_signature("secret-token", url, {"CallSid": "CA123"})
        # Caller received a DIFFERENT CallSid than what was signed -- e.g. a
        # replayed/tampered request body.
        self.assertFalse(validate_signature("secret-token", url, {"CallSid": "CA999"}, sig))

    def test_tampered_url_fails_validation(self):
        sig = compute_signature("secret-token", "https://example.com/real-endpoint", {})
        self.assertFalse(validate_signature("secret-token", "https://example.com/spoofed-endpoint", {}, sig))

    def test_wrong_token_fails_validation(self):
        url = "https://example.com/twiml/inbound-call"
        sig = compute_signature("real-token", url, {})
        self.assertFalse(validate_signature("wrong-token", url, {}, sig))

    def test_missing_signature_fails_closed_not_raises(self):
        self.assertFalse(validate_signature("secret-token", "https://example.com/x", {}, ""))

    def test_missing_auth_token_fails_closed_not_raises(self):
        self.assertFalse(validate_signature("", "https://example.com/x", {}, "anything"))

    def test_case_sensitive_signature_comparison(self):
        url = "https://example.com/x"
        sig = compute_signature("secret-token", url, {})
        self.assertFalse(validate_signature("secret-token", url, {}, sig.lower() if sig.isupper() else sig.upper()))


class TestIsValidationConfigured(unittest.TestCase):
    def test_empty_token_means_not_configured(self):
        self.assertFalse(is_validation_configured(""))

    def test_none_like_empty_string_means_not_configured(self):
        self.assertFalse(is_validation_configured(None or ""))

    def test_real_token_means_configured(self):
        self.assertTrue(is_validation_configured("a-real-looking-token"))


if __name__ == "__main__":
    unittest.main()
