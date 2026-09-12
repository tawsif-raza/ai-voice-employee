"""
Twilio Media Streams Telephony Protocol Models (src/voice/telephony_models.py)

Encapsulates data models, event schemas, validation, and session lifecycle tracking
for real-time bidirectional telephone audio streaming via Twilio Media Streams.

Specifications:
- Codec: audio/x-mulaw (G.711 μ-law)
- Sample Rate: 8000 Hz
- Channels: 1 (Mono)
- Chunk Size: 20ms (160 raw bytes, base64 encoded inside JSON envelope)
"""

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class TwilioEventType(str, Enum):
    CONNECTED = "connected"
    START = "start"
    MEDIA = "media"
    DTMF = "dtmf"
    MARK = "mark"
    CLEAR = "clear"
    STOP = "stop"


class CallStatus(str, Enum):
    INITIALIZING = "initializing"
    CONNECTED = "connected"
    STREAMING = "streaming"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"


class TwilioProtocolError(ValueError):
    """Raised when an incoming Twilio WebSocket frame violates specification."""


@dataclass(frozen=True)
class TwilioStartData:
    account_sid: str
    stream_sid: str
    call_sid: str
    tracks: list[str]
    media_format: dict[str, Any]
    custom_parameters: dict[str, str] = field(default_factory=dict)

    def validate_audio_format(self) -> None:
        encoding = self.media_format.get("encoding", "").lower()
        rate = self.media_format.get("sampleRate", 0)
        channels = self.media_format.get("channels", 0)

        if "mulaw" not in encoding:
            raise TwilioProtocolError(
                f"Unsupported audio encoding '{encoding}'. Twilio Media Streams requires 'audio/x-mulaw'."
            )
        if rate != 8000:
            raise TwilioProtocolError(f"Unsupported sample rate '{rate}'. Expected 8000 Hz for telephony.")
        if channels != 1:
            raise TwilioProtocolError(f"Unsupported channel count '{channels}'. Expected 1 (mono).")


@dataclass(frozen=True)
class TwilioMediaData:
    stream_sid: str
    payload_base64: str
    timestamp: Optional[str] = None
    chunk: Optional[str] = None

    def decode_raw_bytes(self) -> bytes:
        """Decode base64 payload into raw 8kHz μ-law audio bytes."""
        try:
            return base64.b64decode(self.payload_base64)
        except Exception as exc:
            raise TwilioProtocolError(f"Malformed base64 audio payload: {exc}") from exc


def parse_twilio_frame(raw_json: dict[str, Any]) -> tuple[TwilioEventType, Any]:
    """
    Parse and strictly validate an incoming JSON frame from Twilio.
    Returns (event_type, parsed_data_object).
    """
    event_str = raw_json.get("event")
    if not event_str:
        raise TwilioProtocolError("Missing 'event' field in Twilio frame.")

    try:
        event_type = TwilioEventType(event_str)
    except ValueError:
        raise TwilioProtocolError(f"Unrecognized Twilio event type: '{event_str}'")

    if event_type == TwilioEventType.CONNECTED:
        protocol = raw_json.get("protocol", "Call")
        return event_type, {"protocol": protocol, "version": raw_json.get("version", "1.0.0")}

    elif event_type == TwilioEventType.START:
        start_dict = raw_json.get("start")
        if not isinstance(start_dict, dict):
            raise TwilioProtocolError("Missing or invalid 'start' object in Twilio START event.")

        data = TwilioStartData(
            account_sid=start_dict.get("accountSid", ""),
            stream_sid=start_dict.get("streamSid") or raw_json.get("streamSid", ""),
            call_sid=start_dict.get("callSid", ""),
            tracks=start_dict.get("tracks", ["inbound"]),
            media_format=start_dict.get("mediaFormat", {}),
            custom_parameters=dict(start_dict.get("customParameters", {}) or {}),
        )
        data.validate_audio_format()
        return event_type, data

    elif event_type == TwilioEventType.MEDIA:
        media_dict = raw_json.get("media")
        if not isinstance(media_dict, dict):
            raise TwilioProtocolError("Missing or invalid 'media' object in Twilio MEDIA event.")

        stream_sid = raw_json.get("streamSid", "")
        payload = media_dict.get("payload", "")
        if not payload:
            raise TwilioProtocolError("Empty media payload received from Twilio.")

        return event_type, TwilioMediaData(
            stream_sid=stream_sid,
            payload_base64=payload,
            timestamp=media_dict.get("timestamp"),
            chunk=media_dict.get("chunk"),
        )

    elif event_type == TwilioEventType.DTMF:
        dtmf_dict = raw_json.get("dtmf", {})
        return event_type, {
            "stream_sid": raw_json.get("streamSid", ""),
            "digit": dtmf_dict.get("digit", ""),
        }

    elif event_type == TwilioEventType.MARK:
        mark_dict = raw_json.get("mark", {})
        return event_type, {
            "stream_sid": raw_json.get("streamSid", ""),
            "name": mark_dict.get("name", ""),
        }

    elif event_type == TwilioEventType.STOP:
        stop_dict = raw_json.get("stop", {})
        return event_type, {
            "stream_sid": raw_json.get("streamSid", ""),
            "call_sid": stop_dict.get("callSid", ""),
            "account_sid": stop_dict.get("accountSid", ""),
        }

    return event_type, raw_json


def build_media_message(stream_sid: str, raw_mulaw_bytes: bytes) -> dict[str, Any]:
    """
    Construct a valid Twilio outbound 'media' JSON event containing base64 audio.
    Ensure raw_mulaw_bytes is pure 8kHz μ-law without RIFF/WAV headers.
    """
    payload_b64 = base64.b64encode(raw_mulaw_bytes).decode("ascii")
    return {
        "event": "media",
        "streamSid": stream_sid,
        "media": {
            "payload": payload_b64,
        },
    }


def build_mark_message(stream_sid: str, mark_name: str) -> dict[str, Any]:
    """Construct a Twilio outbound 'mark' JSON event for playback tracking."""
    return {
        "event": "mark",
        "streamSid": stream_sid,
        "mark": {
            "name": mark_name,
        },
    }


def build_clear_message(stream_sid: str) -> dict[str, Any]:
    """
    Construct a Twilio outbound 'clear' JSON event.
    CRITICAL FOR BARGE-IN: Instructs Twilio to immediately discard all
    unplayed buffered audio from its local playback queue.
    """
    return {
        "event": "clear",
        "streamSid": stream_sid,
    }


@dataclass
class CallSession:
    """
    Isolated state container for a single active phone call.
    Guarantees strict state separation between concurrent callers.
    """

    call_sid: str
    stream_sid: str
    session_id: str
    user_id: Optional[str] = None
    caller_id: Optional[str] = None
    status: CallStatus = CallStatus.INITIALIZING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    turn_count: int = 0
    active_turn_id: int = 0
    is_interrupted: bool = False
    conversation_history: list[dict[str, str]] = field(default_factory=list)
    interrupted_turns: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def next_turn(self) -> int:
        self.turn_count += 1
        self.active_turn_id = self.turn_count
        self.is_interrupted = False
        return self.active_turn_id

    def interrupt_current_turn(self) -> None:
        self.is_interrupted = True
        self.status = CallStatus.INTERRUPTED

    def record_turn_completed(
        self,
        user_text: str,
        assistant_text: str,
        interrupted: bool = False,
        partial_spoken: str = "",
    ) -> None:
        """
        Record turn completion in conversation history with explicit
        interruption markers so multi-turn context accurately reflects what
        the caller actually heard before cut-off.
        """
        if interrupted:
            spoken = partial_spoken.strip()
            summary = (
                f"[Interrupted by caller after speaking: '{spoken}']"
                if spoken
                else "[Interrupted by caller before speaking]"
            )
            self.conversation_history.append({"role": "user", "content": user_text})
            self.conversation_history.append({"role": "assistant", "content": summary})
            self.interrupted_turns.append(
                {
                    "turn_id": self.active_turn_id,
                    "user_text": user_text,
                    "partial_spoken": spoken,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
        else:
            self.conversation_history.append({"role": "user", "content": user_text})
            self.conversation_history.append({"role": "assistant", "content": assistant_text.strip()})

    def masked_caller_id(self) -> str:
        """Return privacy-safe masked caller phone number for logging."""
        if not self.caller_id:
            return "anonymous"
        cid = str(self.caller_id)
        if len(cid) <= 4:
            return "****"
        return f"{cid[:2]}****{cid[-4:]}"
