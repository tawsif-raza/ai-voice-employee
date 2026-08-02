"""
Phase 10: Text-to-Speech Client
Goal: Stream the voice assistant's reply from the inference API
      (src/api/server.py) and speak it with ElevenLabs as soon as each
      sentence is ready, instead of waiting for the full reply to finish
      generating first.
"""

import argparse
import io
import json
import os
import sys

import pygame
import requests
from elevenlabs import stream as elevenlabs_stream
from elevenlabs.client import ElevenLabs

API_URL = os.environ.get("VOICE_API_URL", "http://localhost:8000/generate")
ELEVENLABS_MODEL_ID = "eleven_turbo_v2_5"
# ElevenLabs' commonly-referenced premade voice ("Rachel"). Override with
# --voice-id or $ELEVENLABS_VOICE_ID for a voice available on your account.
DEFAULT_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
SENTENCE_ENDINGS = (".", "!", "?", "\n")


# ── Streaming client for the inference API ──────────────────────────────────

def iter_text_chunks(message: str, history=None, api_url: str = API_URL):
    """
    POST to the inference API with stream=True and yield (kind, payload)
    pairs as NDJSON lines arrive over the wire:
      ("token", str)  -- a piece of generated text
      ("done", dict)  -- final {"response", "is_handoff", "latency_ms"}
    """
    payload = {"message": message, "history": history or [], "stream": True}
    with requests.post(api_url, json=payload, stream=True, timeout=120) as response:
        response.raise_for_status()
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            event = json.loads(line)
            if event.get("done"):
                yield "done", event
            else:
                yield "token", event.get("token", "")


class SentenceChunker:
    """
    Buffers streamed text tokens and releases complete sentences as soon
    as a sentence-ending character arrives, so TTS can start speaking the
    first sentence while the model is still generating the rest.
    """

    def __init__(self):
        self._buffer = ""

    def feed(self, token: str) -> list[str]:
        self._buffer += token
        sentences = []
        while True:
            cut = self._find_break(self._buffer)
            if cut is None:
                break
            sentence, self._buffer = self._buffer[:cut].strip(), self._buffer[cut:]
            if sentence:
                sentences.append(sentence)
        return sentences

    def flush(self) -> str:
        remainder = self._buffer.strip()
        self._buffer = ""
        return remainder

    @staticmethod
    def _find_break(text: str) -> int | None:
        for i, ch in enumerate(text):
            if ch in SENTENCE_ENDINGS:
                return i + 1
        return None


# ── Text-to-speech playback ──────────────────────────────────────────────────

def play_with_pygame(audio_bytes: bytes) -> None:
    if not audio_bytes:
        return
    if not pygame.mixer.get_init():
        pygame.mixer.init()
    pygame.mixer.music.load(io.BytesIO(audio_bytes))
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy():
        pygame.time.wait(50)


def synthesize_and_play(client: ElevenLabs, text: str, voice_id: str, player: str) -> None:
    if not text.strip():
        return
    print(f"  [TTS] \"{text}\"")
    audio_stream = client.text_to_speech.stream(
        voice_id=voice_id,
        text=text,
        model_id=ELEVENLABS_MODEL_ID,
        output_format="mp3_44100_128",
    )
    if player == "elevenlabs":
        # ElevenLabs' native player — requires mpv installed on the host.
        elevenlabs_stream(audio_stream)
    else:
        # Default: play via pygame-ce, which is already a project dependency
        # and needs no external binary.
        play_with_pygame(b"".join(chunk for chunk in audio_stream if chunk))


# ── Chat loop ────────────────────────────────────────────────────────────────

def chat_once(
    client: ElevenLabs,
    message: str,
    history: list[dict],
    voice_id: str,
    player: str,
    api_url: str,
) -> dict:
    chunker = SentenceChunker()
    final = None

    print("Assistant: ", end="", flush=True)
    for kind, payload in iter_text_chunks(message, history=history, api_url=api_url):
        if kind == "token":
            print(payload, end="", flush=True)
            for sentence in chunker.feed(payload):
                synthesize_and_play(client, sentence, voice_id, player)
        else:
            final = payload
    print()

    remainder = chunker.flush()
    if remainder:
        synthesize_and_play(client, remainder, voice_id, player)

    if final is None:
        raise RuntimeError("Stream ended without a final 'done' event from the API.")

    if final["is_handoff"]:
        print("\n[!] HANDOFF TRIGGERED — route this conversation to a human agent.\n")

    print(f"({final['latency_ms']:.0f} ms)\n")
    return final


def run_interactive(client: ElevenLabs, voice_id: str, player: str, api_url: str) -> None:
    print("\nVoice Assistant (TTS) — type 'exit' or Ctrl+C to quit.\n")
    history: list[dict] = []

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            break

        final = chat_once(client, user_input, history, voice_id, player, api_url)
        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": final["response"]})


# ── Entry point ────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stream voice-assistant replies through ElevenLabs TTS")
    parser.add_argument("--message", default=None, help="Single message to send, then exit. Omit for interactive chat.")
    parser.add_argument("--api-url", default=API_URL, help="Inference API /generate endpoint.")
    parser.add_argument("--voice-id", default=DEFAULT_VOICE_ID, help="ElevenLabs voice ID to speak with.")
    parser.add_argument(
        "--player",
        choices=["pygame", "elevenlabs"],
        default="pygame",
        help="Audio backend: pygame-ce (default, no external binary) or "
             "ElevenLabs' native player (requires mpv on PATH).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("ERROR: ELEVENLABS_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    eleven_client = ElevenLabs(api_key=api_key)

    if args.message:
        chat_once(eleven_client, args.message, [], args.voice_id, args.player, args.api_url)
    else:
        run_interactive(eleven_client, args.voice_id, args.player, args.api_url)
