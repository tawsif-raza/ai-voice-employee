# Automated Canary & Regression Testing Guide

## 1. Full Regression Baseline Test Suite

The baseline repository contains 763 unit, integration, and security tests.
All new telephony tests are strictly additive.

To run the complete automated test suite:
```bash
python -m unittest discover -s tests -p "test_*.py"
```
**Expected Output**:
```
Ran 780+ tests in ~65s
OK
```

---

## 2. Telephony & Voice Test Suites

To run only the voice pipeline, benchmark, and canary test suites:
```bash
python -m unittest tests/test_voice_*.py tests/test_latency_tracker.py
```

### Specific Canary Test Suites:
| Test File | Milestone Criterion Covered | Focus Area |
| :--- | :--- | :--- |
| `tests/test_latency_tracker.py` | Criterion 3 | LatencyTracker calculations (p50, p95, max) |
| `tests/test_voice_canary_safety.py` | Criterion 5 | Unsafe medical query, prompt injection, interim transcript isolation |
| `tests/test_voice_canary_failover.py` | Criterion 4 | Claude 429 quota exhaustion -> Gemini fallback |
| `tests/test_voice_canary_concurrency.py` | Criterion 6 | 5 concurrent telephone calls, session isolation, stream isolation |
| `tests/test_voice_canary_barge_in_improved.py` | Criterion 7 | Mid-turn interruption, clear frame dispatch, confirmation invalidation |
| `tests/test_voice_canary_failures.py` | Criterion 8 | Twilio disconnect, STT timeout, TTS error, caller silence |
| `tests/test_voice_pipeline.py` | Core Telephony | Inbound frames, audio conversion, basic barge-in |
| `tests/test_voice_server_integration.py` | API Server | TwiML XML endpoints, WebSocket `/ws/call` protocol |

---

## 3. Testing with Mock vs. Real Services

### Offline / Mock Testing Mode:
Set the environment variable:
```bash
export VOICE_MOCK_SERVICES=true
```
In this mode:
- `MockSTTService` and `MockTTSService` are used.
- No outbound API calls to Deepgram, ElevenLabs, Anthropic, or Google are made.
- Unit tests run in milliseconds without network dependency or API costs.

### Live Canary Verification Mode:
Set the environment variable:
```bash
export VOICE_MOCK_SERVICES=false
```
Provide active API keys:
- `DEEPGRAM_API_KEY`
- `ELEVENLABS_API_KEY`
- `ANTHROPIC_API_KEY`
- `GEMINI_API_KEY`

---

## 4. Latency Report Generation

To generate an executive latency report against canary run logs:
```bash
python scripts/latency_report.py [path_to_canary_log.json]
```
If no file path is given, it demonstrates against standard SLA thresholds:
- `total_turn_latency`: p95 <= 850ms
- `barge_in_detection_latency`: p95 <= 150ms
- `claude_ttft`: p95 <= 300ms
- `gemini_ttft`: p95 <= 350ms
- `tts_first_audio_latency`: p95 <= 150ms
- `audio_clear_latency`: p95 <= 50ms