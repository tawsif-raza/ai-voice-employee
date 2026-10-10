"""
Local end-to-end telephony simulation (REAL_TELEPHONY_READINESS.md, "what can be tested locally").

Runs the REAL server (src/api/server.py under uvicorn) with the REAL provider
adapters -- DeepgramSTTService (WebSocket), GeminiLLMProvider / GroqLLMProvider
(HTTP SSE via requests), ElevenLabsTTSService (httpx) -- pointed at local fake
provider endpoints, and drives it with a fake Twilio client speaking the real
Media Streams protocol (connected / start / 20 ms mu-law media frames / stop).

Everything here is SIMULATED: no real audio, phone network, Twilio, Deepgram,
ElevenLabs or Gemini/Groq. What it measures is the application's own pipeline
behaviour and overhead with the production deadlines from configs/reliability.yaml
(only the call-duration scenario shortens MAX_CALL_DURATION_SECONDS).

    APP_ENV=dev python scripts/telephony_simulation.py [--only NAME ...] [--json out.json]

No credentials are used or needed; the fake keys below are sentinels that the
log scan at the end checks never appear in the output.
"""

import argparse
import asyncio
import base64
import dataclasses
import http.server
import io
import json
import logging
import os
import queue
import socket
import statistics
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _sub in ("src/agent", "src/inference", "src/voice", "src/api"):
    sys.path.insert(0, str(ROOT / _sub))

SENTINELS = {
    "GEMINI_API_KEY": "AIzaSIMULATION_SENTINEL_gemini",
    "GROQ_API_KEY": "gsk_SIMULATION_SENTINEL_groq",
    "DEEPGRAM_API_KEY": "dg_SIMULATION_SENTINEL_deepgram",
    "ELEVENLABS_API_KEY": "el_SIMULATION_SENTINEL_elevenlabs",
    # Turns Twilio signature checks ON (the fake Twilio client signs its
    # WebSocket handshake) and lets the server make the safety-fallback
    # call-update request (to a fake Twilio REST transport, never the network).
    "TWILIO_AUTH_TOKEN": "twilio_SIMULATION_SENTINEL_token",
}
SIM_ACCOUNT_SID = "AC" + "5" * 32  # fictional, well-formed Twilio SIDs
URGENT_CALL_SID = "CA" + "e" * 32
CALLER_NUMBER = "+15550001234"  # reserved fictional number; must not appear unmasked in logs

os.environ.setdefault("APP_ENV", "dev")
os.environ.update(SENTINELS)
os.environ.update({"LLM_PROVIDER": "free_fallback", "PERSISTENCE_MODE": "dev", "VOICE_MOCK_SERVICES": "false"})
# TWILIO_AUTH_TOKEN stays set (sentinel): every simulated call signs its
# handshake, as real Twilio does, and the safety fallback can authenticate.
os.environ.pop("ANTHROPIC_API_KEY", None)

LOG = io.StringIO()
_handler = logging.StreamHandler(LOG)
_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
logging.getLogger().addHandler(_handler)
logging.getLogger().setLevel(logging.INFO)

import httpx  # noqa: E402
import llm_provider  # noqa: E402
import server  # noqa: E402
import stt_service  # noqa: E402
import tts_service  # noqa: E402
import uvicorn  # noqa: E402
import websockets  # noqa: E402
from twilio_signature import compute_signature  # noqa: E402

now = time.monotonic


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── fake LLM (Gemini + Groq, real HTTP/SSE) ─────────────────────────────────
class LLMScript:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.gemini = {"mode": "ok", "ttft": 0.0, "gap": 0.0}
        self.groq = {"mode": "ok", "ttft": 0.0, "gap": 0.0}
        self.answers: dict[str, str] = {}
        self.default_answer = "We are open from nine to five on weekdays."
        self.events: list[tuple[float, str, str]] = []  # (t, provider, what)

    def answer_for(self, body: dict) -> str:
        # Match triggers against the LATEST user message only (the body also
        # carries the conversation history).
        if "contents" in body:  # Gemini
            text = " ".join(p.get("text", "") for p in body["contents"][-1].get("parts", []))
        else:  # Groq / OpenAI-compatible
            text = body.get("messages", [{}])[-1].get("content", "")
        for trigger, ans in self.answers.items():
            if trigger in text:
                return ans
        return self.default_answer


LLM = LLMScript()


class FakeLLMHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        provider = "gemini" if "streamGenerateContent" in self.path else "groq"
        cfg = dict(getattr(LLM, provider))
        with LLM.lock:
            LLM.events.append((now(), provider, "request"))
        mode = cfg["mode"]
        if mode == "error":
            self._plain(500, b'{"error":{"message":"simulated outage"}}')
            return
        if mode.startswith("hang"):
            time.sleep(float(mode.split(":")[1]))
            self._plain(504, b'{"error":{"message":"simulated hang ended"}}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        time.sleep(cfg["ttft"])
        words = [] if mode == "empty" else LLM.answer_for(body).split(" ")
        try:
            for i, w in enumerate(words):
                chunk = w + (" " if i < len(words) - 1 else "")
                if i == 0:
                    with LLM.lock:
                        LLM.events.append((now(), provider, "first_text"))
                if provider == "gemini":
                    line = {"candidates": [{"content": {"parts": [{"text": chunk}]}}]}
                else:
                    line = {"choices": [{"delta": {"content": chunk}}]}
                self.wfile.write(b"data: " + json.dumps(line).encode() + b"\n\n")
                self.wfile.flush()
                if cfg["gap"]:
                    time.sleep(cfg["gap"])
            if provider == "groq":
                self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def _plain(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


# ── fake Deepgram (real WebSocket) ──────────────────────────────────────────
class DeepgramScript:
    """Each fake Deepgram connection has its own command queue; commands go to the newest live one."""

    def __init__(self):
        self.live: list = []
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.mode = "ok"  # ok | refuse
        self.connections = 0
        self.frames = 0
        self.auth_ok = True

    def _send(self, cmd, everyone=False):
        with self.lock:
            targets = list(self.live) if everyone else self.live[-1:]
        for q in targets:
            q.put(cmd)

    def say(self, text):
        self._send(("final", text, now()))

    def say_all(self, text):
        self._send(("final", text, now()), everyone=True)

    def speech_started(self):
        self._send(("speech_started", None, now()))

    def drop(self):
        self._send(("drop", None, now()))


DG = DeepgramScript()


async def fake_deepgram(connection):
    DG.connections += 1
    commands: "queue.Queue[tuple]" = queue.Queue()
    with DG.lock:
        DG.live.append(commands)
    if connection.request.headers.get("Authorization") != f"Token {SENTINELS['DEEPGRAM_API_KEY']}":
        DG.auth_ok = False

    async def pump_commands():
        while True:
            try:
                kind, text, _t = commands.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.005)
                continue
            if kind == "final":
                await connection.send(
                    json.dumps(
                        {
                            "type": "Results",
                            "is_final": True,
                            "speech_final": True,
                            "channel": {"alternatives": [{"transcript": text, "confidence": 0.97}]},
                        }
                    )
                )
            elif kind == "speech_started":
                await connection.send(json.dumps({"type": "SpeechStarted"}))
            elif kind == "drop":
                await connection.close(code=1011)
                return

    pump = asyncio.create_task(pump_commands())
    try:
        async for message in connection:
            if isinstance(message, bytes):
                DG.frames += 1
            elif json.loads(message).get("type") == "CloseStream":
                break
    except websockets.ConnectionClosed:
        pass
    finally:
        pump.cancel()
        with DG.lock:
            if commands in DG.live:
                DG.live.remove(commands)


async def deepgram_process_request(connection, request):
    if DG.mode == "refuse":
        return connection.respond(503, "simulated outage\n")
    return None


# ── fake ElevenLabs (httpx transport into the real adapter) ─────────────────
class ElevenLabsScript:
    def __init__(self):
        self.reset()

    def reset(self):
        self.mode = "ok"  # ok | fail_next:N | always_fail | hang:S
        self.latency = 0.0
        self.requests: list[tuple[float, str]] = []
        self.auth_ok = True


EL = ElevenLabsScript()

# ── fake Twilio REST (call-update requests: the safety fallback) ────────────
TWILIO_REST: list[dict] = []


def fake_twilio_rest(request: httpx.Request) -> httpx.Response:
    import xml.etree.ElementTree as ET

    twiml = httpx.QueryParams(request.content.decode()).get("Twiml", "")
    root = ET.fromstring(twiml)
    TWILIO_REST.append(
        {
            "path": request.url.path,
            "authed": request.headers.get("authorization", "").startswith("Basic "),
            "verbs": [child.tag for child in root],
            "say": root.find("Say").text if root.find("Say") is not None else "",
        }
    )
    return httpx.Response(200, json={"sid": URGENT_CALL_SID, "status": "in-progress"})


async def fake_elevenlabs(request: httpx.Request) -> httpx.Response:
    text = json.loads(request.content).get("text", "")
    EL.requests.append((now(), text))
    if request.headers.get("xi-api-key") != SENTINELS["ELEVENLABS_API_KEY"]:
        EL.auth_ok = False
    if EL.mode == "always_fail":
        return httpx.Response(503, text="simulated outage")
    if EL.mode.startswith("fail_next:"):
        left = int(EL.mode.split(":")[1])
        EL.mode = f"fail_next:{left - 1}" if left > 1 else "ok"
        return httpx.Response(500, text="simulated failure")
    if EL.mode.startswith("hang:"):
        await asyncio.sleep(float(EL.mode.split(":")[1]))
    if EL.latency:
        await asyncio.sleep(EL.latency)
    tag = (len(EL.requests) % 200) + 30  # each clause's audio carries its own byte value
    return httpx.Response(200, content=bytes([tag]) * 800)  # 100 ms of mu-law


# ── fake Twilio client ──────────────────────────────────────────────────────
class TwilioCall:
    def __init__(self, port: int, call_sid: str):
        self.url = f"ws://127.0.0.1:{port}/ws/call"
        self.call_sid, self.stream_sid = call_sid, "MZ" + call_sid[2:]
        self.received: list[tuple[float, dict]] = []
        self.closed_at = None
        self.close_code = None
        self.send_media = True

    async def __aenter__(self):
        self.dg_baseline = DG.connections
        # Signed the way real Twilio signs the Media Streams handshake (the
        # server verifies it: TWILIO_AUTH_TOKEN is set above).
        signature = compute_signature(SENTINELS["TWILIO_AUTH_TOKEN"], self.url.replace("ws://", "http://", 1), {})
        self.ws = await websockets.connect(self.url, additional_headers={"X-Twilio-Signature": signature})
        self.connected_at = now()
        await self.ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        await self.ws.send(
            json.dumps(
                {
                    "event": "start",
                    "streamSid": self.stream_sid,
                    "start": {
                        "accountSid": SIM_ACCOUNT_SID,
                        "streamSid": self.stream_sid,
                        "callSid": self.call_sid,
                        "tracks": ["inbound"],
                        "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                        "customParameters": {"From": CALLER_NUMBER},
                    },
                }
            )
        )
        self._tasks = [asyncio.create_task(self._media_loop()), asyncio.create_task(self._recv_loop())]
        return self

    async def _media_loop(self):
        payload = base64.b64encode(b"\xff" * 160).decode()
        seq = 0
        try:
            while True:
                if self.send_media:
                    seq += 1
                    await self.ws.send(
                        json.dumps(
                            {
                                "event": "media",
                                "streamSid": self.stream_sid,
                                "media": {
                                    "track": "inbound",
                                    "chunk": str(seq),
                                    "timestamp": str(seq * 20),
                                    "payload": payload,
                                },
                            }
                        )
                    )
                await asyncio.sleep(0.02)
        except websockets.ConnectionClosed:
            pass

    async def _recv_loop(self):
        try:
            async for raw in self.ws:
                self.received.append((now(), json.loads(raw)))
        except websockets.ConnectionClosed:
            pass
        finally:
            self.closed_at = now()
            self.close_code = self.ws.close_code

    async def stt_ready(self, timeout=5):
        """Wait until THIS call's own fake-Deepgram connection is open."""
        return await wait_for(lambda: DG.connections > self.dg_baseline, timeout)

    def media_after(self, t):
        return [(ts, m) for ts, m in self.received if ts >= t and m.get("event") == "media"]

    def first_media_after(self, t):
        media = self.media_after(t)
        return media[0][0] if media else None

    def events_after(self, t, name):
        return [ts for ts, m in self.received if ts >= t and m.get("event") == name]

    async def wait_closed(self, timeout):
        deadline = now() + timeout
        while self.closed_at is None and now() < deadline:
            await asyncio.sleep(0.05)
        return self.closed_at

    async def hangup(self):
        if self.closed_at is None:
            try:
                await self.ws.send(
                    json.dumps({"event": "stop", "streamSid": self.stream_sid, "stop": {"callSid": self.call_sid}})
                )
                await self.ws.close()
            except websockets.ConnectionClosed:
                pass
        for t in self._tasks:
            t.cancel()

    async def __aexit__(self, *exc):
        await self.hangup()


async def wait_for(predicate, timeout, step=0.02):
    deadline = now() + timeout
    while now() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


def counters():
    return server._metrics.snapshot()["counters"]


# ── scenarios ───────────────────────────────────────────────────────────────
RESULTS = []


def record(name, passed, details, **measurements):
    RESULTS.append({"scenario": name, "result": "PASS" if passed else "FAIL", "details": details, **measurements})
    print(f"{'PASS' if passed else 'FAIL'}  {name}: {details}", flush=True)


async def scenario_basic_turns(port):
    """Several normal turns: measure each stage; history continuity."""
    stages = {
        "speech_end_to_llm_request": [],
        "llm_first_text_to_tts_request": [],
        "tts_request_to_first_media": [],
        "speech_end_to_first_media": [],
    }
    LLM.answers = {
        "return": "Unused items can be returned within thirty days.",
        "parking": "Yes, there is free parking behind the building.",
        "repeat": "Of course. Unused items can be returned within thirty days.",
    }
    async with TwilioCall(port, "CAbasic") as call:
        connected = await call.stt_ready()
        utterances = [
            "What is your return policy for online orders?",
            "Is there parking nearby?",
            "Could you repeat that please?",
        ]
        ok = connected
        for text in utterances:
            await asyncio.sleep(0.3)
            mark = len(LLM.events)
            el_mark = len(EL.requests)
            t_end = now()
            DG.say(text)
            got = await wait_for(lambda t_end=t_end: call.first_media_after(t_end) is not None, 10)
            await asyncio.sleep(0.4)
            ok &= got
            llm = [e for e in LLM.events[mark:]]
            t_req = next((t for t, p, w in llm if w == "request"), None)
            t_first = next((t for t, p, w in llm if w == "first_text"), None)
            t_tts = EL.requests[el_mark][0] if len(EL.requests) > el_mark else None
            t_media = call.first_media_after(t_end)
            if None not in (t_req, t_first, t_tts, t_media):
                stages["speech_end_to_llm_request"].append((t_req - t_end) * 1000)
                stages["llm_first_text_to_tts_request"].append((t_tts - t_first) * 1000)
                stages["tts_request_to_first_media"].append((t_media - t_tts) * 1000)
                stages["speech_end_to_first_media"].append((t_media - t_end) * 1000)
            else:
                ok = False
        await asyncio.sleep(0.3)
        marks = call.events_after(0, "mark")
    summary = {k: round(statistics.median(v), 1) for k, v in stages.items() if v}
    record(
        "basic_turns",
        ok and len(marks) >= 3 and DG.auth_ok and EL.auth_ok,
        f"3 turns answered with audio; {len(marks)} end-of-turn marks; median ms {summary}",
        latency_ms=summary,
    )


async def scenario_clinical(port):
    LLM.answers = {}
    async with TwilioCall(port, "CAclinical") as call:
        await call.stt_ready()
        mark = len(LLM.events)
        t_end = now()
        DG.say("What dose should I take of ibuprofen with my warfarin?")
        got = await wait_for(lambda: call.first_media_after(t_end) is not None, 10)
        await asyncio.sleep(0.5)
        spoken = " ".join(t for _ts, t in EL.requests[-3:])
    llm_called = len(LLM.events) > mark
    record(
        "clinical_guard_on_voice",
        got and not llm_called and "pharmacist" in spoken,
        f"handoff spoken={got}, LLM called={llm_called}",
    )


async def scenario_urgent(port):
    """docs/CLINICAL_SAFETY.md: a possible emergency gets the emergency instruction first, no LLM."""
    LLM.answers = {}
    async with TwilioCall(port, "CAurgent") as call:
        await call.stt_ready()
        mark, el_mark = len(LLM.events), len(EL.requests)
        t_end = now()
        DG.say("I'm having trouble breathing after taking my medication.")
        got = await wait_for(lambda: call.first_media_after(t_end) is not None, 10)
        await asyncio.sleep(0.5)
        spoken = [t for _ts, t in EL.requests[el_mark:]]
        first_media_ms = round((call.first_media_after(t_end) - t_end) * 1000, 1) if got else None
    llm_called = len(LLM.events) > mark
    first = spoken[0].lower() if spoken else ""
    record(
        "urgent_risk_on_voice",
        got and not llm_called and "emergency" in first,
        f"emergency instruction spoken first={'emergency' in first}, LLM called={llm_called}, "
        f"speech end -> first audio {first_media_ms} ms",
        latency_ms={"speech_end_to_first_media": first_media_ms},
    )


async def scenario_decision_router(port):
    """docs/DECISION_ROUTING.md: a greeting and a thanks are spoken without any LLM request (FAQ CACHE is off)."""
    LLM.answers = {}
    latencies = {}
    ok = True
    async with TwilioCall(port, "CArouter") as call:
        ok &= await call.stt_ready()
        for name, text in (("greeting", "Hello"), ("thanks", "Thank you")):
            await asyncio.sleep(0.3)
            mark = len(LLM.events)
            t_end = now()
            DG.say(text)
            got = await wait_for(lambda t_end=t_end: call.first_media_after(t_end) is not None, 10)
            await asyncio.sleep(0.4)
            ok &= got and len(LLM.events) == mark
            if got:
                latencies[f"{name}_speech_end_to_first_media"] = round(
                    (call.first_media_after(t_end) - t_end) * 1000, 1
                )
    record(
        "decision_router_shortcut_on_voice",
        ok,
        f"greeting + thanks spoken with no LLM request; ms {latencies}",
        latency_ms=latencies,
    )


async def scenario_barge_in(port):
    LLM.answers = {
        "story": " ".join(["This is a long answer that keeps going so that the caller can interrupt it."] * 6)
    }
    LLM.gemini["gap"] = 0.08
    EL.latency = 0.05
    ok_runs = 0
    details = []
    for attempt in range(3):
        async with TwilioCall(port, f"CAbarge{attempt}") as call:
            await call.stt_ready()
            await asyncio.sleep(0.2)
            t0 = now()
            DG.say("Tell me the long story please")
            await wait_for(lambda call=call, t0=t0: len(call.media_after(t0)) >= 3, 10)
            el_before = len(EL.requests)
            t_interrupt = now()
            DG.speech_started()
            DG.say("Actually what are your opening hours?")
            LLM.answers["opening"] = "We are open from nine to five on weekdays."
            cleared = await wait_for(
                lambda call=call, t_interrupt=t_interrupt: call.events_after(t_interrupt, "clear"), 3
            )
            await asyncio.sleep(2.0)
            t_clear = (call.events_after(t_interrupt, "clear") or [None])[0]
            new_texts = [t for _ts, t in EL.requests[el_before:]]
            old_after = [t for t in new_texts if "long answer" in t]
            new_spoken = any("nine to five" in t for t in new_texts)
            history = None
            handler = server._voice_call_manager.get_handler(call.stream_sid)
            if handler:
                history = [m["content"] for m in handler.session.conversation_history]
            hist_ok = history is not None and history[-1].startswith("We are open")
            run_ok = bool(cleared) and new_spoken and len(old_after) <= 1 and hist_ok
            ok_runs += run_ok
            details.append(
                f"run{attempt}: clear {round((t_clear - t_interrupt) * 1000, 1) if t_clear else None} ms, "
                f"old-turn clauses synthesized after interrupt={len(old_after)}, new answer spoken={new_spoken}, history ok={hist_ok}"
            )
    LLM.gemini["gap"] = 0.0
    EL.latency = 0.0
    record("barge_in", ok_runs == 3, "; ".join(details))


async def _single_turn(port, call_sid, utterance, wait=25, until=None):
    """One utterance; waits until `until` (a phrase) has been sent to TTS, or any audio if None."""
    async with TwilioCall(port, call_sid) as call:
        await call.stt_ready()
        await asyncio.sleep(0.2)
        el_mark, llm_mark = len(EL.requests), len(LLM.events)
        t_end = now()
        DG.say(utterance)
        if until is None:
            await wait_for(lambda: call.first_media_after(t_end) is not None and len(EL.requests) > el_mark, wait)
        else:
            await wait_for(lambda: any(until in x for _t, x in EL.requests[el_mark:]) or call.closed_at, wait)
        await asyncio.sleep(1.0)
        call.open_before_hangup = call.closed_at is None
        return call, t_end, [(round((t - t_end), 2), x) for t, x in EL.requests[el_mark:]], LLM.events[llm_mark:]


async def scenario_llm_failures(port):
    LLM.answers = {}
    # Gemini error -> Groq
    LLM.gemini["mode"] = "error"
    _, _, spoken, ev = await _single_turn(port, "CAgemerr", "What is your return policy for online orders?")
    groq_used = any(p == "groq" and w == "first_text" for _t, p, w in ev)
    record(
        "gemini_error_falls_back_to_groq",
        groq_used and any("nine to five" in s for _t, s in spoken),
        f"groq answered={groq_used}; spoken={[s for _t, s in spoken]}",
    )
    # Gemini empty -> Groq
    LLM.gemini["mode"] = "empty"
    _, _, spoken, ev = await _single_turn(port, "CAgemempty", "What is your return policy for online orders?")
    groq_used = any(p == "groq" and w == "first_text" for _t, p, w in ev)
    record("gemini_empty_treated_as_failure", groq_used, f"groq answered={groq_used}; spoken={[s for _t, s in spoken]}")
    # Both unavailable -> conversation layer's fixed apology/handoff text
    LLM.gemini["mode"] = LLM.groq["mode"] = "error"
    _, _, spoken, _ = await _single_turn(port, "CAbothdown", "What is your return policy for online orders?")
    words = " ".join(s for _t, s in spoken)
    record("both_llms_down_not_silent", "trouble" in words, f"spoken={[s for _t, s in spoken]}")
    # Gemini hang -> filler at ~4 s, Groq fallback after Gemini's own 30 s timeout would exceed 15 s deadline
    LLM.groq["mode"] = "ok"
    LLM.gemini["mode"] = "hang:40"
    call, t_end, spoken, _ = await _single_turn(
        port, "CAgemhang", "What is your return policy for online orders?", wait=25, until="say that again"
    )
    filler = next((t for t, s in spoken if "One moment" in s), None)
    apology = next((t for t, s in spoken if "say that again" in s), None)
    record(
        "gemini_hang_hits_deadlines",
        filler is not None and apology is not None,
        f"filler spoken at {filler}s, apology at {apology}s after speech end (config: filler 4 s, first-token 15 s)",
        filler_s=filler,
        apology_s=apology,
    )
    LLM.reset()


async def scenario_tts_failures(port):
    LLM.answers = {}
    EL.mode = "fail_next:1"
    call, _t, spoken, _ = await _single_turn(
        port, "CAtts1", "What is your return policy for online orders?", until="say that again"
    )
    still_open = call.open_before_hangup
    record(
        "elevenlabs_temporary_failure",
        still_open and any("say that again" in s for _t2, s in spoken),
        f"call still open={still_open}; spoken={[s for _t2, s in spoken]}",
    )
    EL.mode = "always_fail"
    rest_mark = len(TWILIO_REST)
    async with TwilioCall(port, "CAttsdead") as call:
        await call.stt_ready()
        await asyncio.sleep(0.2)
        t_end = now()
        DG.say("What is your return policy for online orders?")
        closed = await call.wait_closed(15)
    redirected = len(TWILIO_REST) > rest_mark
    record(
        "elevenlabs_permanent_failure_closes_stream",
        closed is not None and call.close_code == 1000 and not redirected,
        f"stream closed by service after {round(closed - t_end, 2) if closed else None}s, code {call.close_code}; "
        f"safety redirect requested={redirected} (ordinary call: the inbound TwiML <Say> applies -- real Twilio "
        "playback NOT verifiable here)",
    )
    EL.mode = "ok"


async def scenario_urgent_tts_down(port):
    """docs/CLINICAL_SAFETY.md, "Voice: when TTS fails": the emergency instruction survives an ElevenLabs outage."""
    LLM.answers = {}
    EL.mode = "always_fail"
    mark, rest_mark = len(LLM.events), len(TWILIO_REST)
    async with TwilioCall(port, URGENT_CALL_SID) as call:
        await call.stt_ready()
        await asyncio.sleep(0.2)
        t_end = now()
        DG.say("I'm having trouble breathing after taking my medicine.")
        closed = await call.wait_closed(15)
    EL.mode = "ok"
    redirects = TWILIO_REST[rest_mark:]
    said = redirects[0]["say"] if redirects else ""
    llm_called = len(LLM.events) > mark
    ok = (
        closed is not None
        and len(redirects) == 1
        and redirects[0]["authed"]
        and redirects[0]["path"].endswith(f"/Calls/{URGENT_CALL_SID}.json")
        and redirects[0]["verbs"] == ["Say", "Hangup"]
        and "emergency" in said.lower()
        and "call back later" not in said.lower()
        and not llm_called
    )
    record(
        "urgent_with_tts_down_uses_urgent_fallback",
        ok,
        f"call-update requests={len(redirects)}, LLM called={llm_called}, stream closed after "
        f"{round(closed - t_end, 2) if closed else None}s; Twilio told to say: {said[:70]!r}... then hang up "
        "(real Twilio playback NOT verifiable here)",
    )


async def scenario_stt_failures(port):
    DG.mode = "refuse"
    async with TwilioCall(port, "CAsttdown") as call:
        closed = await call.wait_closed(15)
        elapsed = round(closed - call.connected_at, 2) if closed else None
    record(
        "deepgram_unreachable_at_start",
        closed is not None and call.close_code == 1000,
        f"stream closed after {elapsed}s, code {call.close_code}",
    )
    DG.mode = "ok"
    # Drop mid-call, reconnect succeeds
    async with TwilioCall(port, "CAsttdrop") as call:
        await call.stt_ready()
        before = DG.connections
        DG.drop()
        reconnected = await wait_for(lambda: DG.connections > before, 6)
        await asyncio.sleep(0.3)
        t_end = now()
        DG.say("What is your return policy for online orders?")
        answered = await wait_for(lambda: call.first_media_after(t_end) is not None, 10)
    record(
        "deepgram_drop_reconnects",
        reconnected and answered,
        f"reconnected={reconnected}, answered after reconnect={answered}",
    )
    # Drop, then every reconnect refused -> bounded give-up -> stream closed
    async with TwilioCall(port, "CAsttexh") as call:
        await call.stt_ready()
        start_conns = DG.connections
        DG.mode = "refuse"
        t0 = now()
        DG.drop()
        closed = await call.wait_closed(30)
        attempts = DG.connections - start_conns
    DG.mode = "ok"
    record(
        "deepgram_reconnect_exhaustion_ends_call",
        closed is not None and call.close_code == 1000,
        f"stream closed {round(closed - t0, 2) if closed else None}s after the drop, code {call.close_code} (reconnect attempts reached fake: {attempts})",
    )


async def scenario_dead_media(port):
    before = counters().get("voice_calls_inactivity_ended_total", 0)
    async with TwilioCall(port, "CAdead") as call:
        await call.stt_ready()
        t_stop = now()
        call.send_media = False
        closed = await call.wait_closed(40)
    after = counters().get("voice_calls_inactivity_ended_total", 0)
    elapsed = round(closed - t_stop, 2) if closed else None
    record(
        "dead_media_closed_at_inactivity_timeout",
        closed is not None and 19 <= elapsed <= 23 and after == before + 1,
        f"closed {elapsed}s after media stopped (config 20 s), inactivity metric +{after - before}",
        dead_media_s=elapsed,
    )


async def scenario_call_duration(port):
    original = server._SECURITY
    server._SECURITY = dataclasses.replace(original, max_call_duration_seconds=5)
    before = counters()
    try:
        async with TwilioCall(port, "CAduration") as call:
            closed = await call.wait_closed(20)
            elapsed = round(closed - call.connected_at, 2) if closed else None
    finally:
        server._SECURITY = original
    after = counters()
    dur = after.get("voice_calls_duration_limited_total", 0) - before.get("voice_calls_duration_limited_total", 0)
    inact = after.get("voice_calls_inactivity_ended_total", 0) - before.get("voice_calls_inactivity_ended_total", 0)
    record(
        "call_duration_limit",
        closed is not None and 4.5 <= elapsed <= 7 and dur == 1 and inact == 0,
        f"closed after {elapsed}s (shortened limit 5 s), duration metric +{dur}, inactivity metric +{inact}",
    )


async def scenario_consecutive_failures(port):
    LLM.gemini["mode"] = LLM.groq["mode"] = "hang:40"
    async with TwilioCall(port, "CAconsec") as call:
        await call.stt_ready()
        timeline = []
        for i in range(3):
            if call.closed_at:
                break
            el_mark = len(EL.requests)
            t_end = now()
            DG.say(f"Question number {i + 1}")
            await wait_for(
                lambda el_mark=el_mark: (
                    any("say that again" in t for _ts, t in EL.requests[el_mark:]) or call.closed_at is not None
                ),
                25,
            )
            apologised = any("say that again" in t for _ts, t in EL.requests[el_mark:])
            timeline.append(
                f"failure {i + 1}: apology={apologised}, closed={call.closed_at is not None} at +{round(now() - t_end, 1)}s"
            )
            await asyncio.sleep(0.5)
        closed = await call.wait_closed(5)
    LLM.reset()
    record(
        "three_consecutive_failures_end_call",
        closed is not None and call.close_code == 1000 and len(timeline) == 3 and "apology=True" in timeline[0],
        "; ".join(timeline) + f"; final close code {call.close_code}",
    )


async def scenario_concurrent_calls(port):
    LLM.answers = {}
    calls = [TwilioCall(port, f"CAconc{i}") for i in range(5)]
    for c in calls:
        await c.__aenter__()
    await wait_for(lambda: DG.connections >= 5, 5)
    t_end = now()
    DG.say_all("What is your return policy for online orders?")
    answered = await wait_for(lambda: all(c.first_media_after(t_end) for c in calls), 15)
    active = server._active_call_connections
    for c in calls:
        await c.hangup()
    released = await wait_for(lambda: server._active_call_connections == 0, 5)
    record(
        "five_concurrent_calls",
        answered and active == 5 and released,
        f"all answered={answered}, active during={active}, slots released={released}",
    )


SCENARIOS = {
    "basic_turns": scenario_basic_turns,
    "clinical": scenario_clinical,
    "urgent": scenario_urgent,
    "decision_router": scenario_decision_router,
    "barge_in": scenario_barge_in,
    "llm_failures": scenario_llm_failures,
    "tts_failures": scenario_tts_failures,
    "urgent_tts_down": scenario_urgent_tts_down,
    "stt_failures": scenario_stt_failures,
    "dead_media": scenario_dead_media,
    "call_duration": scenario_call_duration,
    "consecutive_failures": scenario_consecutive_failures,
    "concurrent_calls": scenario_concurrent_calls,
}


def mirror_production_image(rag: bool) -> None:
    """docker/Dockerfile.production disables RAG; mirror that unless --rag is given."""
    if rag:
        return
    import conversation_manager

    original = conversation_manager._load_rag_config

    def production_rag_config():
        return {**original(), "enabled": False}

    conversation_manager._load_rag_config = production_rag_config


def start_fake_providers():
    llm_port = free_port()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", llm_port), FakeLLMHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    llm_provider.GeminiLLMProvider.API_BASE = f"http://127.0.0.1:{llm_port}/v1beta/models"
    llm_provider.GroqLLMProvider.API_URL = f"http://127.0.0.1:{llm_port}/openai/v1/chat/completions"

    dg_port = free_port()
    ready = threading.Event()

    def run_dg():
        async def main():
            async with websockets.serve(fake_deepgram, "127.0.0.1", dg_port, process_request=deepgram_process_request):
                ready.set()
                await asyncio.Future()

        asyncio.run(main())

    threading.Thread(target=run_dg, daemon=True).start()
    ready.wait(5)
    real_build = stt_service.DeepgramSTTService._build_ws_url

    def local_url(self):
        return f"ws://127.0.0.1:{dg_port}/v1/listen?" + real_build(self).split("?", 1)[1]

    stt_service.DeepgramSTTService._build_ws_url = local_url
    server.ElevenLabsTTSService = lambda: tts_service.ElevenLabsTTSService(
        transport=httpx.MockTransport(fake_elevenlabs)
    )
    server._twilio_http_transport = httpx.MockTransport(fake_twilio_rest)
    return httpd


def start_app():
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = now() + 60
    while not srv.started and now() < deadline:
        time.sleep(0.05)
    return srv, thread, port


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--json")
    parser.add_argument("--log", help="write the captured application log here (secrets are sentinels)")
    parser.add_argument(
        "--rag", action="store_true", help="keep RAG enabled (full image) instead of the production image's RAG-off"
    )
    args = parser.parse_args()
    mirror_production_image(args.rag)

    httpd = start_fake_providers()
    srv, thread, port = start_app()
    assert srv.started, "server did not start"
    v = server._RELIABILITY.voice
    print(
        f"SIMULATION -- RAG {'ON (full image)' if args.rag else 'OFF (mirrors Dockerfile.production)'}; "
        f"deadlines from config: {dataclasses.asdict(v)}; max call {server._SECURITY.max_call_duration_seconds}s",
        flush=True,
    )
    threads_before = threading.active_count()

    async def run_all():
        for name, fn in SCENARIOS.items():
            if args.only and name not in args.only:
                continue
            LLM.reset()
            DG.reset()
            EL.reset()
            t0 = now()
            try:
                await fn(port)
            except Exception as exc:  # a crash is a failure, never a skip
                record(name, False, f"harness error {type(exc).__name__}: {exc}")
            await wait_for(lambda: server._active_call_connections == 0, 10)
            print(f"      ({name} took {round(now() - t0, 1)}s)", flush=True)

    asyncio.run(run_all())

    time.sleep(1.0)
    snapshot = counters()
    leftovers = {
        "active_call_connections": server._active_call_connections,
        "registered_calls": len(server._voice_call_manager._active_calls),
        "threads_before": threads_before,
        "threads_after": threading.active_count(),
        "voice_turn_workers_alive": sum(1 for t in threading.enumerate() if t.name.startswith("voice-turn")),
    }
    record(
        "resources_released",
        leftovers["active_call_connections"] == 0 and leftovers["registered_calls"] == 0,
        str(leftovers),
        resources=leftovers,
    )

    logs = LOG.getvalue()
    leaked = [k for k, v in SENTINELS.items() if v in logs]
    caller_unmasked = CALLER_NUMBER in logs
    record(
        "logs_free_of_secrets_and_caller_number",
        not leaked and not caller_unmasked,
        f"secrets found: {leaked or 'none'}; unmasked caller number present: {caller_unmasked}; log lines: {logs.count(chr(10))}",
    )
    print(
        "COUNTERS "
        + json.dumps({k: v for k, v in snapshot.items() if v and k.startswith(("voice_", "llm_", "requests_"))}),
        flush=True,
    )

    srv.should_exit = True
    thread.join(timeout=15)
    httpd.shutdown()
    failed = [r["scenario"] for r in RESULTS if r["result"] != "PASS"]
    print(f"RESULT: {'ALL PASS' if not failed else f'{len(failed)} FAILED: {failed}'}", flush=True)
    if args.json:
        Path(args.json).write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")
    if args.log:
        Path(args.log).write_text(logs, encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
