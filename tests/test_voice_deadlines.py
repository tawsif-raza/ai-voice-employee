"""
H3 -- voice reliability: no call can hang, go silent, or hold resources
indefinitely (docs/MASTER_PROJECT_PLAN.md H3, hazards V1-V11).

Handler-level tests drive the real VoiceCallHandler with deterministic fakes
(a scripted ConversationManager, a recording TTS, the mock STT) and
sub-second deadlines. Server-level tests use TestClient where it is faithful
and a real uvicorn server for the idle/duration paths TestClient cannot
exercise (its receive() blocks a worker thread, so wait_for() timeouts only
fire under a real ASGI server).
"""

import asyncio
import dataclasses
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("src/agent", "src/inference", "src/voice", "src/api"):
    sys.path.insert(0, str(_ROOT / _sub))

from llm_provider import BaseLLMProvider, FallbackLLMProvider, GeminiLLMProvider, LLMProviderError  # noqa: E402
from reliability_config import VoiceDeadlines  # noqa: E402
from stt_service import BaseSTTService, MockSTTService, STTEvent, STTEventType  # noqa: E402
from telephony_models import CallSession  # noqa: E402
from tts_service import BaseTTSService, ElevenLabsTTSService, TTSError  # noqa: E402
from voice_pipeline import FILLER_TEXT, TURN_FAILURE_TEXT, VoiceCallHandler  # noqa: E402

MULAW = {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}

FAST = VoiceDeadlines(
    stt_connect_timeout_seconds=0.3,
    filler_after_seconds=0,
    first_token_timeout_seconds=0.3,
    turn_timeout_seconds=1.0,
    fallback_speech_timeout_seconds=0.3,
    max_consecutive_turn_failures=3,
    media_inactivity_timeout_seconds=1.0,
)


# ── fakes ───────────────────────────────────────────────────────────────────
class ScriptedCM:
    """ConversationManager stand-in: each turn runs `script(transcript)` in the worker thread."""

    session_manager = None

    def __init__(self, script):
        self.script = script
        self.closed_generators = 0
        self.calls = 0

    def handle_turn(self, user_input, history=None, auth=None, session_id=None, request_id=None):
        self.calls += 1
        return self._gen(user_input)

    def _gen(self, user_input):
        try:
            yield from self.script(user_input)
        finally:
            self.closed_generators += 1


def answer(text):
    def script(_):
        yield text
        yield {"response": text, "is_handoff": False}

    return script


def hang(seconds, then="late answer"):
    def script(_):
        time.sleep(seconds)
        yield then
        yield {"response": then, "is_handoff": False}

    return script


def trickle_forever(interval=0.05):
    def script(_):
        while True:
            time.sleep(interval)
            yield "word "

    return script


def empty_reply(_):
    yield {"response": "", "is_handoff": False}


def explode(_):
    raise RuntimeError("provider exploded")
    yield  # pragma: no cover


class RecordingTTS(BaseTTSService):
    def __init__(self, hang_seconds=0.0, fail=False):
        self.spoken: list[str] = []
        self.hang_seconds = hang_seconds
        self.fail = fail

    async def synthesize_stream(self, token_stream, cancellation_event=None):
        if self.fail:
            raise TTSError("tts down")
        text = []
        async for token in token_stream:
            if cancellation_event is not None and cancellation_event.is_set():
                break
            if self.hang_seconds:
                await asyncio.sleep(self.hang_seconds)
            text.append(token)
            yield b"\xff" * 160
        if text:  # record only what was actually spoken
            self.spoken.append("".join(text))


def make_handler(cm, tts=None, stt=None, deadlines=FAST, ended=None):
    sent = []

    async def send(msg):
        sent.append(msg)

    async def end_call(reason):
        if ended is not None:
            ended.append(reason)

    handler = VoiceCallHandler(
        session=CallSession(call_sid="CA_H3", stream_sid="MZ_H3", session_id="s_h3", user_id="telephony:CA_H3"),
        send_to_twilio_fn=send,
        conversation_manager=cm,
        stt_service=stt or MockSTTService(),
        tts_service=tts or RecordingTTS(),
        deadlines=deadlines,
        end_call_fn=end_call if ended is not None else None,
    )
    return handler, sent


async def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


# ── V1: LLM deadlines ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_llm_hang_hits_first_token_deadline_and_apologises():
    cm, tts = ScriptedCM(hang(1.5)), RecordingTTS()
    handler, _ = make_handler(cm, tts)

    started = time.monotonic()
    await handler._execute_turn("hello", 1)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, "the turn must give up at the first-token deadline, not wait for the provider"
    assert tts.spoken == [TURN_FAILURE_TEXT]
    assert handler.is_active and handler._consecutive_turn_failures == 1
    # History holds what the caller heard: the apology, not an invented answer.
    assert handler.session.conversation_history[-2:] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": TURN_FAILURE_TEXT},
    ]
    # The worker thread finishes its provider call later and closes the generator.
    assert await wait_until(lambda: cm.closed_generators == 1)


@pytest.mark.asyncio
async def test_filler_is_spoken_while_waiting_but_never_recorded():
    cm, tts = ScriptedCM(hang(0.25, then="We open at nine.")), RecordingTTS()
    handler, _ = make_handler(
        cm, tts, deadlines=dataclasses.replace(FAST, filler_after_seconds=0.1, first_token_timeout_seconds=1.0)
    )

    await handler._execute_turn("when do you open", 1)

    assert tts.spoken == [FILLER_TEXT + "We open at nine."]
    assert handler.session.conversation_history[-1] == {"role": "assistant", "content": "We open at nine."}
    assert handler._consecutive_turn_failures == 0


@pytest.mark.asyncio
async def test_endless_generation_hits_turn_deadline():
    cm, tts = ScriptedCM(trickle_forever()), RecordingTTS()
    handler, _ = make_handler(cm, tts, deadlines=dataclasses.replace(FAST, turn_timeout_seconds=0.5))

    started = time.monotonic()
    await handler._execute_turn("tell me everything", 1)

    assert time.monotonic() - started < 1.5
    assert tts.spoken[-1] == TURN_FAILURE_TEXT
    assert await wait_until(lambda: cm.closed_generators == 1), "the producer must stop and release the generator"


# ── V2: empty / failing replies ─────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("script", [empty_reply, explode], ids=["empty-reply", "provider-error"])
async def test_empty_or_failed_turn_is_never_silent(script):
    tts = RecordingTTS()
    handler, _ = make_handler(ScriptedCM(script), tts)

    await handler._execute_turn("hello", 1)

    assert tts.spoken == [TURN_FAILURE_TEXT]
    assert handler._consecutive_turn_failures == 1


class _EmptyProvider(BaseLLMProvider):
    provider_name = "empty"

    def generate_stream(self, messages, **kwargs):
        raise LLMProviderError("empty response", provider=self.provider_name, retryable=True)
        yield  # pragma: no cover


class _GoodProvider(BaseLLMProvider):
    provider_name = "good"

    def generate_stream(self, messages, **kwargs):
        yield "Fallback answer."
        yield {"text": "Fallback answer.", "latency_ms": 1.0}


def test_gemini_empty_stream_is_a_provider_error(monkeypatch):
    import requests

    class _Resp:
        status_code = 200
        text = ""

        def iter_lines(self, decode_unicode=True):
            return iter(['data: {"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]}'])

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    with pytest.raises(LLMProviderError, match="empty response"):
        list(GeminiLLMProvider(api_key="k").generate_stream([{"role": "user", "content": "hi"}]))


def test_empty_primary_fails_over_to_fallback():
    items = list(FallbackLLMProvider(primary=_EmptyProvider(), fallback=_GoodProvider()).generate_stream([]))
    assert items[0] == "Fallback answer." and items[-1]["fallback_used"] is True


# ── V9: repeated failures end the call ──────────────────────────────────────
@pytest.mark.asyncio
async def test_consecutive_failures_end_the_call():
    ended, tts = [], RecordingTTS()
    handler, _ = make_handler(
        ScriptedCM(explode), tts, deadlines=dataclasses.replace(FAST, max_consecutive_turn_failures=2), ended=ended
    )

    await handler._execute_turn("one", 1)
    assert ended == [] and tts.spoken == [TURN_FAILURE_TEXT]
    await handler._execute_turn("two", 2)
    assert ended == ["repeated_turn_failures"]


@pytest.mark.asyncio
async def test_a_successful_turn_resets_the_failure_count():
    outcomes = iter([explode, answer("Sure."), explode])
    ended = []
    handler, _ = make_handler(
        ScriptedCM(lambda t: next(outcomes)(t)),
        deadlines=dataclasses.replace(FAST, max_consecutive_turn_failures=2),
        ended=ended,
    )

    for turn in (1, 2, 3):
        await handler._execute_turn(f"turn {turn}", turn)

    assert ended == [] and handler._consecutive_turn_failures == 1


# ── V4/V5: TTS failures ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_hanging_tts_apology_is_bounded_and_ends_the_call():
    ended = []
    handler, _ = make_handler(ScriptedCM(explode), RecordingTTS(hang_seconds=5), ended=ended)

    started = time.monotonic()
    await handler._execute_turn("hello", 1)

    assert time.monotonic() - started < 1.5, "speaking the apology must be bounded"
    assert ended == ["fallback_speech_failed"]


@pytest.mark.asyncio
async def test_unspeakable_failure_ends_the_call():
    ended = []
    handler, _ = make_handler(ScriptedCM(answer("Hello there.")), RecordingTTS(fail=True), ended=ended)

    await handler._execute_turn("hello", 1)

    assert ended == ["fallback_speech_failed"]


def _elevenlabs(handler):
    return ElevenLabsTTSService(api_key="el-test-key", transport=httpx.MockTransport(handler))


async def _speak(tts, text="Hello there, this is a test."):
    async def tokens():
        yield text

    return [chunk async for chunk in tts.synthesize_stream(tokens())]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429, 500])
async def test_elevenlabs_error_status_raises_instead_of_silence(status):
    with pytest.raises(TTSError, match=str(status)):
        await _speak(_elevenlabs(lambda request: httpx.Response(status, text="quota exceeded")))


@pytest.mark.asyncio
async def test_elevenlabs_transport_error_raises():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(TTSError, match="ConnectError"):
        await _speak(_elevenlabs(boom))


@pytest.mark.asyncio
async def test_elevenlabs_without_api_key_raises(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(TTSError, match="not configured"):
        await _speak(ElevenLabsTTSService(api_key=""))


@pytest.mark.asyncio
async def test_elevenlabs_success_still_yields_audio():
    wav = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 32 + b"\x7f" * 320
    seen = {}

    def ok(request):
        seen["key"] = request.headers.get("xi-api-key")
        return httpx.Response(200, content=wav)

    chunks = await _speak(_elevenlabs(ok))
    assert chunks == [b"\x7f" * 320] and seen["key"] == "el-test-key"


# ── V3: overlapping turns ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_new_utterance_supersedes_the_running_turn():
    replies = {"first": trickle_forever(0.05), "second": answer("Second answer.")}
    tts = RecordingTTS()
    stt = MockSTTService()
    handler, sent = make_handler(
        ScriptedCM(lambda t: replies[t](t)), tts, stt, deadlines=dataclasses.replace(FAST, turn_timeout_seconds=5)
    )
    await stt.connect()
    loop_task = asyncio.create_task(handler.process_stt_events())

    await stt.push_event(STTEvent(STTEventType.FINAL_TRANSCRIPT, text="first"))
    assert await wait_until(lambda: any(m.get("event") == "media" for m in sent))
    first_task, first_event = handler._active_turn_task, handler._turn_cancellation_event

    await stt.push_event(STTEvent(STTEventType.FINAL_TRANSCRIPT, text="second"))
    assert await wait_until(lambda: handler._active_turn_task is not first_task)
    await handler._active_turn_task

    assert first_event.is_set() and (first_task.cancelled() or first_task.done())
    assert any(m.get("event") == "clear" for m in sent)
    assert handler.session.conversation_history[-1] == {"role": "assistant", "content": "Second answer."}
    loop_task.cancel()
    await handler.handle_stop()


# ── V6/V7: STT ──────────────────────────────────────────────────────────────
class HangingConnectSTT(MockSTTService):
    async def connect(self):
        await asyncio.sleep(10)


class DeadSTT(BaseSTTService):
    async def connect(self):
        raise ConnectionError("deepgram unreachable")

    async def send_audio(self, chunk):
        pass

    async def receive_events(self):
        if False:
            yield None

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_stt_connect_hang_is_bounded_and_ends_the_call():
    ended = []
    handler, _ = make_handler(ScriptedCM(answer("x")), stt=HangingConnectSTT(), ended=ended)

    started = time.monotonic()
    await handler.handle_start(_start_data())

    assert time.monotonic() - started < 1.5
    assert ended == ["stt_connect_failed"]


@pytest.mark.asyncio
async def test_stt_reconnect_exhaustion_ends_the_call(monkeypatch):
    import voice_pipeline

    monkeypatch.setattr(voice_pipeline, "_STT_RECONNECT_BACKOFF_SECONDS", 0.01)
    ended = []
    handler, _ = make_handler(ScriptedCM(answer("x")), stt=DeadSTT(), ended=ended)

    await asyncio.wait_for(handler.process_stt_events(), timeout=5)

    assert ended == ["stt_unavailable"]


def _start_data():
    from telephony_models import TwilioStartData

    return TwilioStartData(
        account_sid="AC_H3",
        stream_sid="MZ_H3",
        call_sid="CA_H3",
        tracks=["inbound"],
        media_format=dict(MULAW),
    )


# ── V11: bounded security counters ──────────────────────────────────────────
def test_security_detector_tracks_a_bounded_number_of_identifiers():
    from audit import AuditLogger, AuditRepository, SecurityEventDetector

    repo = AuditRepository()
    detector = SecurityEventDetector(AuditLogger(repository=repo), max_tracked_identifiers=100)
    for i in range(5000):
        detector.record_auth_failure(f"10.0.{i // 256}.{i % 256}")
    assert detector.tracked_identifier_count() == 100

    for _ in range(3):
        detector.record_auth_failure("attacker")
    assert any(e.type == "REPEATED_AUTH_FAILURE" and e.actor == "attacker" for e in repo.list_security_events())


# ── server: TwiML fallback, jobs backpressure, service-ended calls ──────────
@pytest.fixture
def server_module(monkeypatch):
    import server
    from conversation_manager import build_conversation_manager
    from voice_pipeline import VoiceCallManager

    manager = build_conversation_manager(llm_provider=_GoodProvider(), rag_enabled=False, persistence_enabled=False)
    monkeypatch.setattr(server, "_conversation_manager", manager)
    monkeypatch.setattr(server, "_voice_call_manager", VoiceCallManager(conversation_manager=manager, deadlines=FAST))
    monkeypatch.setattr(server, "_RELIABILITY", dataclasses.replace(server._RELIABILITY, voice=FAST))
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    return server


def test_twiml_continues_with_a_twilio_spoken_fallback(server_module, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server_module, "VOICE_FALLBACK_MESSAGE", "Sorry & goodbye <now>")
    body = TestClient(server_module.app).post("/twiml/inbound-call", data={"CallSid": "CA1"}).text

    assert body.index("</Connect>") < body.index("<Say>")
    assert "<Say>Sorry &amp; goodbye &lt;now&gt;</Say>" in body


def test_jobs_are_rejected_with_503_when_the_queue_is_full(server_module, monkeypatch):
    from fastapi.testclient import TestClient
    from reliability_config import JobsConfig

    monkeypatch.setattr(
        server_module, "_RELIABILITY", dataclasses.replace(server_module._RELIABILITY, jobs=JobsConfig(max_pending=1))
    )
    server_module._job_store.create("already-queued")
    try:
        response = TestClient(server_module.app).post("/jobs/generate", json={"message": "hello"})
    finally:
        server_module._job_store.mark_completed("already-queued", {})
    assert response.status_code == 503 and response.headers.get("Retry-After")


def test_stt_failure_at_call_start_closes_the_stream_and_frees_the_slot(server_module, monkeypatch):
    from fastapi import WebSocketDisconnect
    from fastapi.testclient import TestClient

    monkeypatch.setenv("VOICE_MOCK_SERVICES", "true")
    monkeypatch.setattr(server_module, "MockSTTService", DeadSTT)
    start = {
        "event": "start",
        "streamSid": "MZ_DEAD",
        "start": {
            "streamSid": "MZ_DEAD",
            "callSid": "CA_DEAD",
            "accountSid": "AC1",
            "tracks": ["inbound"],
            "mediaFormat": dict(MULAW),
            "customParameters": {},
        },
    }
    with TestClient(server_module.app).websocket_connect("/ws/call") as ws:
        ws.send_json(start)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_text()
    assert exc_info.value.code == 1000
    assert server_module._active_call_connections == 0
    assert "MZ_DEAD" not in server_module._voice_call_manager._active_calls


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(server_module):
    import uvicorn

    port = _free_port()
    srv = uvicorn.Server(
        uvicorn.Config(server_module.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield server_module, port
    srv.should_exit = True
    thread.join(timeout=10)


async def _closed_after(port):
    import websockets

    started = time.monotonic()
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/call") as ws:
        try:
            await asyncio.wait_for(ws.recv(), timeout=10)
        except websockets.ConnectionClosed as exc:
            return exc.rcvd.code if exc.rcvd else None, time.monotonic() - started
    return None, time.monotonic() - started


def test_dead_media_stream_is_closed_after_inactivity_timeout(live_server):
    server_module, port = live_server
    before = server_module._metrics.snapshot()["counters"].get("voice_calls_inactivity_ended_total", 0)

    code, elapsed = asyncio.run(_closed_after(port))

    assert code == 1000 and 0.8 <= elapsed < 5
    after = server_module._metrics.snapshot()["counters"].get("voice_calls_inactivity_ended_total", 0)
    assert after == before + 1
    assert server_module._active_call_connections == 0


def test_max_call_duration_wins_when_shorter_than_inactivity(live_server, monkeypatch):
    server_module, port = live_server
    monkeypatch.setattr(
        server_module,
        "_SECURITY",
        dataclasses.replace(server_module._SECURITY, max_call_duration_seconds=1),
    )
    monkeypatch.setattr(
        server_module,
        "_RELIABILITY",
        dataclasses.replace(
            server_module._RELIABILITY, voice=dataclasses.replace(FAST, media_inactivity_timeout_seconds=30)
        ),
    )
    before = server_module._metrics.snapshot()["counters"].get("voice_calls_duration_limited_total", 0)

    code, elapsed = asyncio.run(_closed_after(port))

    assert code == 1000 and elapsed < 5
    assert server_module._metrics.snapshot()["counters"].get("voice_calls_duration_limited_total", 0) == before + 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
