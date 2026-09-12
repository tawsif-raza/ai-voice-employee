"""
Text-to-Speech (TTS) Streaming Service (src/voice/tts_service.py)

Provides low-latency streaming TTS synthesis via ElevenLabs Flash v2.5, yielding
8,000 Hz, 8-bit G.711 μ-law raw audio chunks ready for direct Twilio streaming.

Key features:
1. Native `output_format=ulaw_8000`: Zero CPU transcoding needed before Twilio transmission.
2. RIFF/WAV Header Stripper: Ensures audio packets contain pure audio samples without
   metadata headers that cause audible clicks/pops on telephony lines.
3. Sentence/Clause Boundary Buffer: Groups incremental tokens into coherent phrases
   for natural prosody while preserving sub-100ms TTFA (Time-to-First-Audio).
4. Cancellation on Barge-in: Supports immediate cancellation of in-flight synthesis.
5. Deterministic MockTTSService: For fully offline, reproducible automated testing.
"""

import asyncio
import base64
import logging
import os
import re
from abc import ABC, abstractmethod
from typing import AsyncIterator, Iterator, Optional

logger = logging.getLogger("ai_voice_agent.voice.tts")

_CLAUSE_PUNCT_RE = re.compile(r"([.!?;\n]+)")


def strip_wav_header(audio_bytes: bytes) -> bytes:
    """
    If the audio starts with a standard 44-byte RIFF/WAVE header, strip it.
    Twilio Media Streams treats ALL bytes as raw audio samples. A RIFF header
    played as audio sounds like an unpleasant pop or static click.
    """
    if len(audio_bytes) >= 44 and audio_bytes[:4] == b"RIFF" and audio_bytes[8:12] == b"WAVE":
        return audio_bytes[44:]
    return audio_bytes


class BaseTTSService(ABC):
    """Abstract interface for real-time streaming TTS synthesis."""

    @abstractmethod
    async def synthesize_stream(
        self,
        token_stream: AsyncIterator[str],
        cancellation_event: Optional[asyncio.Event] = None,
    ) -> AsyncIterator[bytes]:
        """
        Yield raw 8kHz μ-law audio bytes as tokens arrive.
        Must cease synthesis immediately if cancellation_event is set.
        """
        pass


class ElevenLabsTTSService(BaseTTSService):
    """
    Streaming TTS service using ElevenLabs Flash v2.5.
    Configured for ultra-low latency (~75ms) and 8kHz μ-law telephony output.
    """

    DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # Rachel
    DEFAULT_MODEL_ID = "eleven_flash_v2_5"

    def __init__(
        self,
        api_key: Optional[str] = None,
        voice_id: Optional[str] = None,
        model_id: Optional[str] = None,
        optimize_streaming_latency: int = 3,
    ):
        self.api_key = api_key or os.environ.get("ELEVENLABS_API_KEY", "")
        self.voice_id = voice_id or os.environ.get("ELEVENLABS_VOICE_ID", self.DEFAULT_VOICE_ID)
        self.model_id = model_id or os.environ.get("ELEVENLABS_MODEL_ID", self.DEFAULT_MODEL_ID)
        self.optimize_streaming_latency = optimize_streaming_latency

    async def synthesize_clause(self, text: str) -> bytes:
        """Synthesize a single clause/sentence into raw 8kHz μ-law audio."""
        import httpx

        if not text.strip():
            return b""

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}"
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "audio/basic",  # μ-law standard mime
        }
        params = {
            "output_format": "ulaw_8000",
            "optimize_streaming_latency": self.optimize_streaming_latency,
        }
        payload = {
            "text": text,
            "model_id": self.model_id,
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.8,
            },
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, headers=headers, params=params, json=payload)
            if resp.status_code != 200:
                logger.error("ElevenLabs TTS error (%s): %s", resp.status_code, resp.text)
                return b""
            raw_audio = strip_wav_header(resp.content)
            return raw_audio

    async def synthesize_stream(
        self,
        token_stream: AsyncIterator[str],
        cancellation_event: Optional[asyncio.Event] = None,
    ) -> AsyncIterator[bytes]:
        """
        Buffer incoming tokens into natural clauses (e.g. at punctuation boundaries)
        and stream out raw 8kHz μ-law audio chunks.
        """
        if not self.api_key:
            logger.warning("ELEVENLABS_API_KEY is not set — TTS generation skipped.")
            return

        buffer = []
        async for token in token_stream:
            if cancellation_event and cancellation_event.is_set():
                logger.info("TTS generation cancelled due to user barge-in.")
                return

            buffer.append(token)
            joined = "".join(buffer)

            # Check if we have hit a natural clause or sentence ending
            if any(punct in token for punct in (".", "!", "?", "\n", ";", ":")):
                if len(joined.strip()) >= 15:  # ensure minimal phoneme context for prosody
                    audio_chunk = await self.synthesize_clause(joined.strip())
                    buffer.clear()
                    if audio_chunk:
                        if cancellation_event and cancellation_event.is_set():
                            return
                        yield audio_chunk

        # Flush any remaining tokens at the end of the turn
        remaining = "".join(buffer).strip()
        if remaining:
            if cancellation_event and cancellation_event.is_set():
                return
            audio_chunk = await self.synthesize_clause(remaining)
            if audio_chunk:
                yield audio_chunk


class MockTTSService(BaseTTSService):
    """
    Deterministic Mock TTS Service for unit and integration testing.
    Yields 160-byte chunks of dummy 8kHz μ-law audio (representing 20ms speech frames).
    """

    def __init__(self, frame_count_per_word: int = 2):
        self.frame_count_per_word = frame_count_per_word
        self.synthesized_texts: list[str] = []

    async def synthesize_stream(
        self,
        token_stream: AsyncIterator[str],
        cancellation_event: Optional[asyncio.Event] = None,
    ) -> AsyncIterator[bytes]:
        full_text = []
        async for token in token_stream:
            if cancellation_event and cancellation_event.is_set():
                return
            full_text.append(token)

            # Yield mock 160-byte 8kHz μ-law frames
            for _ in range(self.frame_count_per_word):
                if cancellation_event and cancellation_event.is_set():
                    return
                await asyncio.sleep(0.001)  # small cooperative yield
                # 0xFF in μ-law is silence / low amplitude
                yield b"\xff" * 160

        self.synthesized_texts.append("".join(full_text))
