"""
Contract test for DeepgramSTTService against the real `websockets` library
(docs/MASTER_PROJECT_PLAN.md finding F-02).

Every other STT test uses MockSTTService or the missing-API-key path, so
DeepgramSTTService.connect() had never actually opened a socket. It passed
`extra_headers=` to `websockets.connect()`, a keyword the library's default
(asyncio) client no longer accepts since websockets 14 -- so every real call
failed with a TypeError before reaching Deepgram, and the caller got no
transcription at all.

This test runs a local WebSocket server that speaks the small slice of the
Deepgram streaming protocol the service relies on, and drives the real
service through connect -> send audio -> receive a final transcript ->
close. No network access or Deepgram credentials are needed.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "voice"))

from stt_service import DeepgramSTTService, STTEventType  # noqa: E402

API_KEY = "dg-test-key-not-real"
TRANSCRIPT = "i would like to check my order"

F02 = pytest.mark.xfail(strict=True, reason="F-02: websockets>=14 rejects extra_headers; fixed in H1")


class FakeDeepgram:
    def __init__(self):
        self.auth_header = None
        self.request_path = None
        self.audio_frames: list[bytes] = []
        self.text_frames: list[str] = []

    async def handler(self, connection):
        self.auth_header = connection.request.headers.get("Authorization")
        self.request_path = connection.request.path
        async for message in connection:
            if isinstance(message, bytes):
                self.audio_frames.append(message)
                await connection.send(
                    json.dumps(
                        {
                            "type": "Results",
                            "is_final": True,
                            "speech_final": True,
                            "channel": {"alternatives": [{"transcript": TRANSCRIPT, "confidence": 0.97}]},
                        }
                    )
                )
            else:
                self.text_frames.append(message)
                if json.loads(message).get("type") == "CloseStream":
                    await connection.close()
                    return


@pytest.mark.asyncio
@F02
async def test_deepgram_service_connects_streams_and_closes_against_real_websocket_server(monkeypatch):
    fake = FakeDeepgram()
    async with websockets.serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        service = DeepgramSTTService(api_key=API_KEY)
        real_url = service._build_ws_url()
        query = real_url.split("?", 1)[1]
        monkeypatch.setattr(service, "_build_ws_url", lambda: f"ws://127.0.0.1:{port}/v1/listen?{query}")

        await service.connect()
        await service.send_audio(b"\xff" * 160)

        events = []

        async def _collect():
            async for event in service.receive_events():
                events.append(event)
                if event.event_type == STTEventType.FINAL_TRANSCRIPT:
                    return

        await asyncio.wait_for(_collect(), timeout=5)
        await service.close()

    assert fake.auth_header == f"Token {API_KEY}"
    assert fake.request_path.startswith("/v1/listen?")
    assert "encoding=mulaw" in fake.request_path and "sample_rate=8000" in fake.request_path
    assert fake.audio_frames == [b"\xff" * 160]
    assert [e.event_type for e in events] == [STTEventType.FINAL_TRANSCRIPT]
    assert events[0].text == TRANSCRIPT
    assert fake.text_frames and json.loads(fake.text_frames[-1]) == {"type": "CloseStream"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
