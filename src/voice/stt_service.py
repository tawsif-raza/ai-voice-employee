"""
Speech-to-Text (STT) Streaming Service (src/voice/stt_service.py)

Provides streaming Speech-to-Text via Deepgram WebSocket API, with:
1. Native 8kHz μ-law audio ingestion (zero CPU transcoding from Twilio Media Streams).
2. Instant Voice Activity Detection (VAD) events (`SpeechStarted`) for sub-150ms barge-in.
3. Partial transcripts (`interim_results`) for low-latency observation (never triggering tools).
4. Finalized speech turns (`speech_final`) driving the authoritative ConversationManager.
5. Deterministic MockSTTService for offline unit and integration testing.
"""

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger("ai_voice_agent.voice.stt")


class STTEventType(str, Enum):
    SPEECH_STARTED = "speech_started"      # VAD trigger: Instant barge-in!
    INTERIM_TRANSCRIPT = "interim"         # Partial: Informational only, NEVER triggers tools
    FINAL_TRANSCRIPT = "final"             # Finalized turn: Drives ConversationManager
    UTTERANCE_END = "utterance_end"       # Natural pause boundary
    ERROR = "error"


@dataclass(frozen=True)
class STTEvent:
    event_type: STTEventType
    text: str = ""
    confidence: float = 0.0
    words: list[dict[str, Any]] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)


class BaseSTTService(ABC):
    """Abstract interface for real-time streaming STT."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish the streaming connection to the STT provider."""
        pass

    @abstractmethod
    async def send_audio(self, mulaw_audio_chunk: bytes) -> None:
        """Send a raw 8kHz μ-law audio chunk (typically 20ms / 160 bytes)."""
        pass

    @abstractmethod
    async def receive_events(self) -> AsyncIterator[STTEvent]:
        """Stream parsed STT events as they arrive from the provider."""
        pass

    @abstractmethod
    async def close(self) -> None:
        """Gracefully close the STT streaming connection."""
        pass


class DeepgramSTTService(BaseSTTService):
    """
    Production streaming STT powered by Deepgram Nova-3.
    Uses persistent WebSocket over TLS (`wss://api.deepgram.com/v1/listen`).
    Configured natively for 8000 Hz μ-law audio directly from Twilio.
    """

    DEFAULT_MODEL = "nova-3"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        endpointing_ms: int = 300,
        vad_events: bool = True,
        interim_results: bool = True,
    ):
        self.api_key = api_key or os.environ.get("DEEPGRAM_API_KEY", "")
        self.model = model or os.environ.get("DEEPGRAM_MODEL", self.DEFAULT_MODEL)
        self.endpointing_ms = endpointing_ms
        self.vad_events = vad_events
        self.interim_results = interim_results
        self._ws = None
        self._is_connected = False

    def _build_ws_url(self) -> str:
        base_url = "wss://api.deepgram.com/v1/listen"
        params = [
            f"model={self.model}",
            "encoding=mulaw",
            "sample_rate=8000",
            "channels=1",
            f"endpointing={self.endpointing_ms}",
            "punctuate=true",
            "smart_format=true",
        ]
        if self.vad_events:
            params.append("vad_events=true")
        if self.interim_results:
            params.append("interim_results=true")
        return f"{base_url}?{'&'.join(params)}"

    async def connect(self) -> None:
        import websockets

        if not self.api_key:
            raise ValueError("DEEPGRAM_API_KEY is not configured.")

        url = self._build_ws_url()
        headers = {"Authorization": f"Token {self.api_key}"}
        try:
            self._ws = await websockets.connect(url, extra_headers=headers, ping_interval=10, ping_timeout=10)
            self._is_connected = True
            logger.info("Connected to Deepgram streaming STT (model=%s)", self.model)
        except Exception as exc:
            logger.error("Failed to connect to Deepgram STT: %s", exc)
            self._is_connected = False
            raise

    async def send_audio(self, mulaw_audio_chunk: bytes) -> None:
        if not self._is_connected or not self._ws:
            return
        try:
            # Stream raw binary frame
            await self._ws.send(mulaw_audio_chunk)
        except Exception as exc:
            logger.warning("Error sending audio to Deepgram: %s", exc)
            self._is_connected = False

    async def receive_events(self) -> AsyncIterator[STTEvent]:
        if not self._is_connected or not self._ws:
            return

        try:
            async for message in self._ws:
                if isinstance(message, bytes):
                    continue

                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    continue

                msg_type = payload.get("type")

                # 1. Instant VAD speech detection -> Trigger Barge-In!
                if msg_type == "SpeechStarted":
                    yield STTEvent(event_type=STTEventType.SPEECH_STARTED, raw_payload=payload)
                    continue

                # 2. Utterance end event
                if msg_type == "UtteranceEnd":
                    yield STTEvent(event_type=STTEventType.UTTERANCE_END, raw_payload=payload)
                    continue

                # 3. Transcription results
                if msg_type == "Results":
                    channel = payload.get("channel", {})
                    alternatives = channel.get("alternatives", [])
                    if not alternatives:
                        continue

                    primary_alt = alternatives[0]
                    transcript = primary_alt.get("transcript", "").strip()
                    if not transcript:
                        continue

                    confidence = primary_alt.get("confidence", 0.0)
                    words = primary_alt.get("words", [])
                    is_final = payload.get("is_final", False)
                    speech_final = payload.get("speech_final", False)

                    if speech_final or is_final:
                        # Authoritative finalized speech turn
                        yield STTEvent(
                            event_type=STTEventType.FINAL_TRANSCRIPT,
                            text=transcript,
                            confidence=confidence,
                            words=words,
                            raw_payload=payload,
                        )
                    else:
                        # Partial interim transcript: strictly observational, NEVER triggers tools
                        yield STTEvent(
                            event_type=STTEventType.INTERIM_TRANSCRIPT,
                            text=transcript,
                            confidence=confidence,
                            words=words,
                            raw_payload=payload,
                        )

        except Exception as exc:
            logger.error("Deepgram streaming receive loop encountered error: %s", exc)
            yield STTEvent(event_type=STTEventType.ERROR, text=str(exc))
        finally:
            self._is_connected = False

    async def close(self) -> None:
        if self._ws:
            try:
                # Deepgram protocol: send empty binary chunk to close gracefully
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await self._ws.close()
            except Exception:
                pass
            finally:
                self._ws = None
                self._is_connected = False


class MockSTTService(BaseSTTService):
    """
    Deterministic Mock STT Service for unit and integration testing.
    Can be scripted with predetermined sequences of events.
    """

    def __init__(self, scripted_events: Optional[list[STTEvent]] = None):
        self.scripted_events = scripted_events or []
        self._event_queue: asyncio.Queue[STTEvent] = asyncio.Queue()
        self.received_audio_bytes = bytearray()
        self.is_connected = False

    async def connect(self) -> None:
        self.is_connected = True
        for evt in self.scripted_events:
            await self._event_queue.put(evt)

    async def push_event(self, event: STTEvent) -> None:
        """Push an event dynamically during an active test."""
        await self._event_queue.put(event)

    async def send_audio(self, mulaw_audio_chunk: bytes) -> None:
        self.received_audio_bytes.extend(mulaw_audio_chunk)

    async def receive_events(self) -> AsyncIterator[STTEvent]:
        while self.is_connected or not self._event_queue.empty():
            try:
                evt = await asyncio.wait_for(self._event_queue.get(), timeout=0.1)
                yield evt
            except asyncio.TimeoutError:
                if not self.is_connected:
                    break
                continue

    async def close(self) -> None:
        self.is_connected = False
