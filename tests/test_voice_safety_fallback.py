"""
Voice safety fallback when TTS fails (docs/CLINICAL_SAFETY.md, "Voice: when
TTS fails").

The blocker: an urgent-risk answer ("call your local emergency number") is
spoken through ElevenLabs. With ElevenLabs down, the call used to end on the
inbound TwiML's generic "Please call back later" -- the emergency
instruction never reached the caller. These tests drive the real
ConversationManager (spy Gemini + Groq behind the real FallbackLLMProvider)
through the real VoiceCallHandler and the real /ws/call endpoint, with fake
TTS, scripted STT and a fake Twilio REST transport. No real provider,
credential or phone call is involved; the Twilio SIDs and token are sentinels.
"""

import asyncio
import dataclasses
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("src/agent", "src/inference", "src/voice", "src/api"):
    sys.path.insert(0, str(_ROOT / _sub))

import call_fallback  # noqa: E402
from conversation_manager import ConversationManager, SafetyResponse, build_conversation_manager  # noqa: E402
from llm_provider import BaseLLMProvider, FallbackLLMProvider, LLMProviderError  # noqa: E402
from reliability_config import VoiceDeadlines  # noqa: E402
from stt_service import MockSTTService, STTEvent, STTEventType  # noqa: E402
from telephony_models import CallSession  # noqa: E402
from tts_service import BaseTTSService, TTSError  # noqa: E402
from voice_pipeline import TURN_FAILURE_TEXT, VoiceCallHandler  # noqa: E402

URGENT = ConversationManager.URGENT_SAFETY_RESPONSE
CLINICAL = ConversationManager.CLINICAL_HANDOFF_RESPONSE
URGENT_UTTERANCE = "I'm having trouble breathing after taking my medicine."
MEDICATION_UTTERANCE = "Should I increase my dose?"
NORMAL_UTTERANCE = "What is your return policy for online orders?"
MIXED_UTTERANCE = "Hi, what time do you close? Also my face is swelling after taking the medicine."

ACCOUNT_SID = "AC" + "0" * 32  # sentinel, not a real account
CALL_SID = "CA" + "1" * 32
SENTINEL_TOKEN = "twilio-token-SENTINEL"

FAST = VoiceDeadlines(
    stt_connect_timeout_seconds=0.3,
    filler_after_seconds=0,
    first_token_timeout_seconds=0.3,
    turn_timeout_seconds=1.0,
    fallback_speech_timeout_seconds=0.3,
    max_consecutive_turn_failures=3,
    media_inactivity_timeout_seconds=1.0,
)
UNSAFE_FALLBACK_WORDS = (
    "call back later",
    "connect you",
    "transfer you",
    "transferring",
    "i've called",
    "help is on the way",
)


# ── fakes ───────────────────────────────────────────────────────────────────
class SpyProvider(BaseLLMProvider):
    def __init__(self, name, fail=False):
        self.provider_name, self.fail, self.calls = name, fail, 0

    def generate_stream(self, messages, **kwargs):
        self.calls += 1
        if self.fail:
            raise LLMProviderError(f"{self.provider_name} down", provider=self.provider_name, status_code=503)
        yield "Unused items can be returned within thirty days."
        yield {"text": "Unused items can be returned within thirty days.", "latency_ms": 1.0, "provider": "x"}


class FakeTTS(BaseTTSService):
    """`fail_times` synthesis calls raise TTSError (None = always, i.e. permanently down)."""

    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.attempts = 0
        self.spoken: list[str] = []

    async def synthesize_stream(self, token_stream, cancellation_event=None):
        self.attempts += 1
        failing = self.fail_times is None or self.attempts <= self.fail_times
        text = []
        async for token in token_stream:
            if failing:
                raise TTSError("elevenlabs unavailable")
            text.append(token)
            yield b"\xff" * 160
        if text:
            self.spoken.append("".join(text))


def _chain(gemini_fails=False):
    gemini, groq = SpyProvider("gemini", fail=gemini_fails), SpyProvider("groq")
    return gemini, groq, FallbackLLMProvider(primary=gemini, fallback=groq)


def _handler(tts, gemini_fails=False):
    gemini, groq, chain = _chain(gemini_fails)
    manager = build_conversation_manager(llm_provider=chain, rag_enabled=False, persistence_enabled=False)
    ended: list[str] = []

    async def send(msg):
        return None

    async def end_call(reason):
        ended.append(reason)

    handler = VoiceCallHandler(
        session=CallSession(call_sid=CALL_SID, stream_sid="MZ_SF", session_id="s_sf", user_id="telephony:sf"),
        send_to_twilio_fn=send,
        conversation_manager=manager,
        stt_service=MockSTTService(),
        tts_service=tts,
        deadlines=FAST,
        end_call_fn=end_call,
    )
    return handler, ended, gemini, groq


def _assert_safe_fallback(message):
    lowered = message.lower()
    assert "emergency" in lowered or "pharmacist" in lowered
    for words in UNSAFE_FALLBACK_WORDS:
        assert words not in lowered, words


# ── the safety tag ──────────────────────────────────────────────────────────
def test_safety_responses_are_tagged_but_still_plain_text():
    manager = build_conversation_manager(
        llm_provider=SpyProvider("gemini"), rag_enabled=False, persistence_enabled=False
    )
    urgent = next(iter(manager.handle_turn(URGENT_UTTERANCE)))
    medication = next(iter(manager.handle_turn(MEDICATION_UTTERANCE)))
    assert isinstance(urgent, SafetyResponse) and urgent.category == "urgent" and urgent == URGENT
    assert isinstance(medication, SafetyResponse) and medication.category == "medication" and medication == CLINICAL
    assert not hasattr(next(iter(manager.handle_turn("Hello"))), "category")


# ── A-G: the handler with each TTS outcome ──────────────────────────────────
@pytest.mark.asyncio
async def test_a_urgent_with_working_tts_speaks_the_emergency_instruction():
    tts = FakeTTS()
    handler, ended, gemini, groq = _handler(tts)
    await handler._execute_turn(URGENT_UTTERANCE, 1)
    assert tts.spoken == [URGENT] and ended == []
    assert gemini.calls == groq.calls == 0
    assert handler.service_fallback_tier() == "urgent"  # sticky for any later service-side ending


@pytest.mark.asyncio
async def test_b_urgent_with_permanently_failed_tts_ends_on_the_urgent_fallback():
    tts = FakeTTS(fail_times=None)
    handler, ended, gemini, groq = _handler(tts)
    await handler._execute_turn(URGENT_UTTERANCE, 1)
    assert tts.spoken == [] and ended == ["safety_speech_failed"]
    assert handler.service_fallback_tier() == "urgent"
    assert gemini.calls == groq.calls == 0
    assert TURN_FAILURE_TEXT not in tts.spoken
    _assert_safe_fallback(call_fallback.fallback_message("urgent", ""))


@pytest.mark.asyncio
async def test_c_urgent_with_a_temporary_tts_failure_retries_the_emergency_text_and_keeps_the_call():
    tts = FakeTTS(fail_times=1)
    handler, ended, gemini, groq = _handler(tts)
    await handler._execute_turn(URGENT_UTTERANCE, 1)
    # The retry speaks the emergency instruction itself, not "say that again".
    assert tts.spoken == [URGENT] and ended == []
    assert handler._consecutive_turn_failures == 1  # H3 counting unchanged
    assert gemini.calls == groq.calls == 0


@pytest.mark.asyncio
async def test_d_ordinary_call_with_failed_tts_keeps_the_ordinary_fallback():
    tts = FakeTTS(fail_times=None)
    handler, ended, gemini, groq = _handler(tts)
    await handler._execute_turn(NORMAL_UTTERANCE, 1)
    assert ended == ["fallback_speech_failed"]
    assert handler.service_fallback_tier() is None  # generic TwiML <Say>, unchanged
    assert gemini.calls == 1


@pytest.mark.asyncio
async def test_e_medication_question_with_failed_tts_never_reaches_the_llm():
    tts = FakeTTS(fail_times=None)
    handler, ended, gemini, groq = _handler(tts)
    await handler._execute_turn(MEDICATION_UTTERANCE, 1)
    assert ended == ["safety_speech_failed"] and handler.service_fallback_tier() == "medication"
    assert gemini.calls == groq.calls == 0
    _assert_safe_fallback(call_fallback.fallback_message("medication", ""))


@pytest.mark.asyncio
async def test_e_delivered_medication_answer_needs_no_special_fallback():
    handler, ended, _, _ = _handler(FakeTTS())
    await handler._execute_turn(MEDICATION_UTTERANCE, 1)
    assert ended == [] and handler.service_fallback_tier() is None


@pytest.mark.asyncio
async def test_f_mixed_admin_and_urgent_request_with_failed_tts_is_urgent():
    tts = FakeTTS(fail_times=None)
    handler, ended, gemini, groq = _handler(tts)
    await handler._execute_turn(MIXED_UTTERANCE, 1)
    assert handler.service_fallback_tier() == "urgent" and ended == ["safety_speech_failed"]
    assert gemini.calls == groq.calls == 0


@pytest.mark.asyncio
async def test_g_gemini_failure_falls_back_to_groq_but_never_for_safety_turns():
    handler, _, gemini, groq = _handler(FakeTTS(), gemini_fails=True)
    await handler._execute_turn(NORMAL_UTTERANCE, 1)
    assert gemini.calls == 1 and groq.calls == 1  # ordinary turn: fallback works

    tts = FakeTTS(fail_times=None)
    handler, ended, gemini, groq = _handler(tts, gemini_fails=True)
    await handler._execute_turn(URGENT_UTTERANCE, 1)
    assert gemini.calls == groq.calls == 0 and ended == ["safety_speech_failed"]


@pytest.mark.asyncio
async def test_urgent_stays_sticky_when_a_later_ordinary_turn_fails():
    tts = FakeTTS()
    handler, ended, _, _ = _handler(tts)
    await handler._execute_turn(URGENT_UTTERANCE, 1)
    tts.fail_times, tts.attempts = None, 0  # TTS dies afterwards
    await handler._execute_turn(NORMAL_UTTERANCE, 2)
    assert ended == ["fallback_speech_failed"] and handler.service_fallback_tier() == "urgent"


# ── the TwiML itself ────────────────────────────────────────────────────────
@pytest.mark.parametrize("tier", ["urgent", "medication"])
def test_fallback_twiml_is_valid_says_the_safety_message_and_hangs_up(tier):
    message = call_fallback.fallback_message(tier, "")
    root = ET.fromstring(call_fallback.build_fallback_twiml(message))
    assert root.tag == "Response" and [child.tag for child in root] == ["Say", "Hangup"]
    assert root[0].text == message  # one message, spoken once
    _assert_safe_fallback(root[0].text)


def test_fallback_twiml_escapes_a_configured_message():
    message = call_fallback.fallback_message("urgent", "Call 000 <now> & stay on the line")
    root = ET.fromstring(call_fallback.build_fallback_twiml(message))
    assert root[0].text == "Call 000 <now> & stay on the line"


def test_ordinary_calls_have_no_safety_fallback_and_an_empty_override_keeps_the_safe_default():
    assert call_fallback.fallback_message(None, "anything") is None
    assert call_fallback.fallback_message("urgent", "   ") == call_fallback.DEFAULT_URGENT_FALLBACK_MESSAGE


# ── the Twilio call-update request ──────────────────────────────────────────
class TwilioRecorder:
    """Fake Twilio REST: answers `status`, or raises `raises` (an exception instance)."""

    def __init__(self, status=200, explode=False, raises=None):
        self.status, self.requests = status, []
        self.raises = raises or (httpx.ConnectError("twilio unreachable") if explode else None)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        return httpx.Response(self.status, json={"message": f"echo {SENTINEL_TOKEN}"} if self.status >= 400 else {})


# Every way the Twilio call-update request can end (Phase 8 contract).
REST_OUTCOMES = {
    "success-200": dict(status=200),
    "timeout": dict(raises=httpx.ReadTimeout("twilio slow")),
    "connect-error": dict(raises=httpx.ConnectError("twilio unreachable")),
    "bad-request-400": dict(status=400),
    "unauthorized-401": dict(status=401),
    "not-found-404": dict(status=404),
    "server-error-500": dict(status=500),
    "unavailable-503": dict(status=503),
    "unexpected-exception": dict(raises=RuntimeError("client bug")),
}


def _redirect(recorder, **kwargs):
    args = dict(auth_token=SENTINEL_TOKEN, transport=httpx.MockTransport(recorder))
    args.update(kwargs)
    twiml = call_fallback.build_fallback_twiml(call_fallback.DEFAULT_URGENT_FALLBACK_MESSAGE)
    return asyncio.run(call_fallback.redirect_call(ACCOUNT_SID, CALL_SID, twiml, **args))


def test_redirect_posts_the_twiml_to_the_calls_resource_with_basic_auth():
    recorder = TwilioRecorder()
    assert _redirect(recorder) is True
    (request,) = recorder.requests
    assert request.method == "POST"
    assert str(request.url) == f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Calls/{CALL_SID}.json"
    assert request.headers["authorization"].startswith("Basic ")
    body = httpx.QueryParams(request.content.decode())
    assert ET.fromstring(body["Twiml"])[0].text == call_fallback.DEFAULT_URGENT_FALLBACK_MESSAGE


@pytest.mark.parametrize("outcome", list(REST_OUTCOMES), ids=list(REST_OUTCOMES))
def test_redirect_contract_never_raises_and_never_logs_the_token(outcome, caplog):
    recorder = TwilioRecorder(**REST_OUTCOMES[outcome])
    with caplog.at_level("DEBUG"):
        result = _redirect(recorder)  # must return, never raise
    assert result is (outcome == "success-200")
    assert len(recorder.requests) == 1
    # Neither the token (even when Twilio echoes it back) nor the TwiML is logged.
    assert SENTINEL_TOKEN not in caplog.text
    assert "<Say>" not in caplog.text and "Response" not in caplog.text


@pytest.mark.parametrize("kwargs", [{"auth_token": ""}])
def test_no_request_without_credentials(kwargs):
    recorder = TwilioRecorder()
    assert _redirect(recorder, **kwargs) is False and recorder.requests == []


def test_malformed_sids_never_reach_a_url():
    recorder = TwilioRecorder()
    twiml = call_fallback.build_fallback_twiml("x")
    for account, call in [("AC123/../../x", CALL_SID), (ACCOUNT_SID, "CA1?x=1"), ("", "")]:
        assert (
            asyncio.run(
                call_fallback.redirect_call(
                    account, call, twiml, auth_token="t", transport=httpx.MockTransport(recorder)
                )
            )
            is False
        )
    assert recorder.requests == []


# ── the real /ws/call endpoint ──────────────────────────────────────────────
@pytest.fixture
def voice_server(monkeypatch):
    import server
    from twilio_signature import compute_signature
    from voice_pipeline import VoiceCallManager

    gemini, groq, chain = _chain()
    manager = build_conversation_manager(llm_provider=chain, rag_enabled=False, persistence_enabled=False)
    recorder = TwilioRecorder()
    monkeypatch.setattr(server, "_conversation_manager", manager)
    monkeypatch.setattr(server, "_voice_call_manager", VoiceCallManager(conversation_manager=manager, deadlines=FAST))
    monkeypatch.setattr(server, "_RELIABILITY", dataclasses.replace(server._RELIABILITY, voice=FAST))
    monkeypatch.setattr(server, "_twilio_http_transport", httpx.MockTransport(recorder))
    monkeypatch.setenv("VOICE_MOCK_SERVICES", "true")
    # A configured token turns Twilio signature checks ON: the handshake is
    # signed exactly as the server verifies it (sentinel token).
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", SENTINEL_TOKEN)
    signature = compute_signature(SENTINEL_TOKEN, "http://testserver/ws/call", {})

    def run_call(utterance, tts):
        from fastapi import WebSocketDisconnect
        from fastapi.testclient import TestClient

        events = [STTEvent(event_type=STTEventType.FINAL_TRANSCRIPT, text=utterance, confidence=0.99)]
        monkeypatch.setattr(server, "MockSTTService", lambda: MockSTTService(scripted_events=list(events)))
        monkeypatch.setattr(server, "MockTTSService", lambda: tts)
        start = {
            "event": "start",
            "streamSid": "MZ_SF",
            "start": {
                "streamSid": "MZ_SF",
                "callSid": CALL_SID,
                "accountSid": ACCOUNT_SID,
                "tracks": ["inbound"],
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                "customParameters": {},
            },
        }
        with TestClient(server.app).websocket_connect("/ws/call", headers={"x-twilio-signature": signature}) as ws:
            ws.send_json(start)
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    ws.receive_text()
        return closed.value.code

    return server, recorder, run_call, gemini, groq


def test_urgent_call_with_dead_tts_has_twilio_speak_the_urgent_fallback(voice_server):
    server, recorder, run_call, gemini, groq = voice_server
    code = run_call(URGENT_UTTERANCE, FakeTTS(fail_times=None))

    assert code == 1000  # the media stream is closed by the service
    (request,) = recorder.requests  # exactly one redirect: no duplicate message
    spoken = ET.fromstring(httpx.QueryParams(request.content.decode())["Twiml"])
    assert [child.tag for child in spoken] == ["Say", "Hangup"]
    assert spoken[0].text == server.VOICE_URGENT_FALLBACK_MESSAGE
    _assert_safe_fallback(spoken[0].text)
    assert gemini.calls == groq.calls == 0
    assert server._metrics.snapshot()["counters"].get("voice_safety_fallbacks_total", 0) >= 1


def test_urgent_call_whose_stream_then_dies_still_ends_on_the_urgent_fallback(voice_server):
    # The emergency instruction WAS spoken; then no more media (caller
    # silent / stream dead) -> the inactivity ending is urgent-aware too.
    server, recorder, run_call, _, _ = voice_server
    assert run_call(URGENT_UTTERANCE, FakeTTS()) == 1000
    (request,) = recorder.requests
    assert "emergency" in httpx.QueryParams(request.content.decode())["Twiml"]


def _counter(server, name):
    return server._metrics.snapshot()["counters"].get(name, 0)


def test_successful_safety_fallback_is_counted_as_delivered(voice_server):
    server, recorder, run_call, _, _ = voice_server
    ok, failed = (
        _counter(server, "voice_safety_fallbacks_total"),
        _counter(server, "voice_safety_fallback_failures_total"),
    )
    assert run_call(URGENT_UTTERANCE, FakeTTS(fail_times=None)) == 1000
    assert _counter(server, "voice_safety_fallbacks_total") == ok + 1
    assert _counter(server, "voice_safety_fallback_failures_total") == failed


@pytest.mark.parametrize("outcome", [o for o in REST_OUTCOMES if o != "success-200"])
def test_every_twilio_failure_is_survived_counted_and_still_closes_the_stream(voice_server, outcome, caplog):
    server, recorder, run_call, gemini, groq = voice_server
    failing = TwilioRecorder(**REST_OUTCOMES[outcome])
    recorder.status, recorder.raises = failing.status, failing.raises
    ok, failed = (
        _counter(server, "voice_safety_fallbacks_total"),
        _counter(server, "voice_safety_fallback_failures_total"),
    )

    with caplog.at_level("DEBUG"):
        code = run_call(URGENT_UTTERANCE, FakeTTS(fail_times=None))

    # The server did not crash and still ended the call; ONLY because the
    # update itself failed, Twilio falls back to the inbound TwiML <Say>.
    assert code == 1000 and len(recorder.requests) == 1
    assert _counter(server, "voice_safety_fallback_failures_total") == failed + 1
    assert _counter(server, "voice_safety_fallbacks_total") == ok
    assert "NOT delivered" in caplog.text  # the residual gap is observable
    assert SENTINEL_TOKEN not in caplog.text
    assert gemini.calls == groq.calls == 0


def test_an_unexpected_error_building_the_fallback_still_closes_the_stream(voice_server, monkeypatch):
    server, recorder, run_call, _, _ = voice_server

    def boom(message):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(call_fallback, "build_fallback_twiml", boom)
    failed = _counter(server, "voice_safety_fallback_failures_total")
    assert run_call(URGENT_UTTERANCE, FakeTTS(fail_times=None)) == 1000
    assert recorder.requests == [] and _counter(server, "voice_safety_fallback_failures_total") == failed + 1


def test_max_call_duration_ending_preserves_the_urgent_fallback(voice_server, monkeypatch):
    # The emergency instruction WAS spoken; the call then hits the duration
    # limit (inactivity set long so the duration limit is what binds).
    server, recorder, run_call, _, _ = voice_server
    monkeypatch.setattr(server, "_SECURITY", dataclasses.replace(server._SECURITY, max_call_duration_seconds=1))
    monkeypatch.setattr(
        server,
        "_RELIABILITY",
        dataclasses.replace(server._RELIABILITY, voice=dataclasses.replace(FAST, media_inactivity_timeout_seconds=30)),
    )
    limited = _counter(server, "voice_calls_duration_limited_total")
    assert run_call(URGENT_UTTERANCE, FakeTTS()) == 1000
    assert _counter(server, "voice_calls_duration_limited_total") == limited + 1
    (request,) = recorder.requests
    assert "emergency" in httpx.QueryParams(request.content.decode())["Twiml"]


def test_ordinary_call_with_dead_tts_keeps_the_ordinary_twiml_fallback(voice_server):
    server, recorder, run_call, gemini, _ = voice_server
    assert run_call(NORMAL_UTTERANCE, FakeTTS(fail_times=None)) == 1000
    assert recorder.requests == []  # no redirect: Twilio plays the inbound TwiML <Say>
    assert gemini.calls == 1


def test_inbound_twiml_is_unchanged_valid_and_its_fallback_follows_the_stream(voice_server, monkeypatch):
    from fastapi.testclient import TestClient

    server, _, _, _, _ = voice_server
    monkeypatch.delenv("TWILIO_AUTH_TOKEN")
    root = ET.fromstring(TestClient(server.app).post("/twiml/inbound-call", data={"CallSid": CALL_SID}).text)
    assert [child.tag for child in root] == ["Connect", "Say"]
    assert root[0][0].tag == "Stream" and root[1].text == server.VOICE_FALLBACK_MESSAGE


def test_the_twilio_api_override_is_ignored_outside_dev(monkeypatch):
    # The auth token must never be sent to another host in production.
    import server
    from runtime_env import load_security_settings

    monkeypatch.setenv("TWILIO_API_BASE_URL", "http://attacker.invalid")
    monkeypatch.setattr(
        server, "_SECURITY", load_security_settings({"APP_ENV": "production", "AUTH_MODE": "production"})
    )
    assert server._twilio_api_base_url() == "https://api.twilio.com"
    monkeypatch.setattr(server, "_SECURITY", load_security_settings({"APP_ENV": "dev"}))
    assert server._twilio_api_base_url() == "http://attacker.invalid"
