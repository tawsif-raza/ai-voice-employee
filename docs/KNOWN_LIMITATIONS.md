# Known Architectural Limitations & Technical Debt

This document provides an honest, transparent catalog of known limitations in the current telephony and voice architecture. These items must be understood prior to declaring full production readiness.

---

## 1. Simulated vs. Real-World Telephony Latency

- **In-Process Mock Latency**: The automated benchmark report records an in-process mock turn latency of ~14.9 ms. This is a CPU-only simulated figure using mocked STT and TTS services.
- **Real-World Telephony Latency**: In live canary calls over PSTN/cellular networks:
  - Twilio WebSocket network transit: 30–60 ms.
  - Deepgram Nova-3 acoustic endpointing: 250–350 ms.
  - LLM Time-to-First-Token (Claude 3.5 Haiku): 180–280 ms.
  - ElevenLabs Flash v2.5 synthesis: 75–120 ms.
  - **Actual End-to-End Real Voice Latency**: 550–850 ms.
- **Action**: Never present the 14.9 ms benchmark as real-world caller experience.

---

## 2. Text-to-Speech Streaming Model (HTTP REST vs. Bidirectional WebSocket)

- **Current Implementation**: `ElevenLabsTTSService` synthesizes clauses over HTTPS POST (`https://api.elevenlabs.io/v1/text-to-speech/{voice_id}`) with `output_format=ulaw_8000`.
- **Limitation**: While low-latency (~80ms per clause), it incurs a separate HTTP connection/keep-alive overhead per clause compared to a full-duplex WebSocket connection.
- **Planned Improvement**: Migrate to ElevenLabs WebSocket API (`wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input`) to stream individual tokens directly into audio without punctuation clause buffering.

---

## 3. Voice Activity Detection (VAD) vs. Acoustic Echo Cancellation (AEC)

- **Current Implementation**: Barge-in relies on Deepgram's `SpeechStarted` WebSocket event to dispatch Twilio `clear`.
- **Limitation**: If caller speakerphone volume is set high, the microphone can pick up the assistant's voice and trigger a false-positive barge-in (acoustic feedback loop).
- **Mitigation**: Twilio Media Streams performs server-side AEC on standard calls, but callers using degraded speakerphones may experience occasional false interruptions.

---

## 4. Telephony Identity & Caller Authentication

- **Current Implementation**: Inbound calls are assigned `ANONYMOUS_CONTEXT` with a masked phone number (`caller_id`).
- **Limitation**: No voice biometrics or automatic caller verification is performed before sensitive tool operations (e.g. order cancel or medical record access).
- **Enforcement**: Any write or modification tool requires verbal confirmation before execution.

---

## 5. DTMF Tone Processing

- **Current Implementation**: Twilio `dtmf` events are parsed and logged in `telephony_models.py`.
- **Limitation**: The voice pipeline currently handles only spoken audio; touch-tone DTMF keypad inputs are ignored.