# AI Voice Agent

A production-grade AI voice assistant for customer support, built on a fine-tuned small LLM (Qwen2.5-0.5B with QLoRA), grounded by retrieval-augmented generation (RAG), and hardened with deterministic safety layers.

## Architecture

```
Caller → Telephony → Audio Stream → STT → Conversation Manager → LLM/RAG → TTS → Audio → Caller
                                            ├── Clinical Safety Guard (pre-generation)
                                            ├── Intent Engine (rule-based routing)
                                            ├── Policy Engine (deterministic enforcement)
                                            ├── Tool Orchestrator (action execution)
                                            ├── Session Manager (state persistence)
                                            ├── Memory Manager (conversation memory)
                                            ├── Handoff Detector (post-generation)
                                            ├── Privacy Service (PII detection/redaction)
                                            └── Audit Logger (observability)
```

### Core Principles

1. **Safety over fluency** — deterministic safety checks, not LLM judgment
2. **The LLM never calls tools directly** — text-in/text-out only
3. **Retrieval precedes generation** — RAG grounding, not parametric memory
4. **Fail safe, not silent** — escalate to human on uncertainty
5. **Configuration over code** — YAML-driven detection and routing

## Project Structure

```
src/
├── agent/                  # Core agent modules
│   ├── conversation_manager.py   # Turn orchestration (the hub)
│   ├── intent_engine.py          # Rule-based intent classification
│   ├── policy_engine.py          # Deterministic policy enforcement
│   ├── tool_orchestrator.py      # Validated tool execution
│   ├── session_manager.py        # Session state & lifecycle
│   ├── memory_manager.py         # Conversation memory (sliding window + summary)
│   ├── identity.py               # Authentication providers
│   ├── oidc_provider.py          # Production OIDC/JWT validation
│   ├── pii_detector.py           # PII pattern detection
│   ├── privacy_service.py        # Privacy enforcement
│   ├── audit.py                  # Audit logging
│   ├── metrics.py                # Metrics collection
│   ├── reliability.py            # Retry policies & circuit breakers
│   ├── db.py                     # Database foundation (PostgreSQL/SQLite)
│   └── mock_tools.py             # Mock business tools
├── api/
│   └── server.py                 # FastAPI inference API
├── inference/
│   ├── llm_service.py            # Model loading & streaming generation
│   ├── handoff_detector.py       # Layered handoff-intent detection
│   └── predict.py                # Backward-compatible inference facade
├── rag/
│   ├── retriever.py              # FAISS-backed retrieval
│   ├── knowledge_base.py         # Knowledge loading & chunking
│   ├── embeddings.py             # Sentence-transformer embeddings
│   └── build_index.py            # Index builder
├── voice/
│   └── client_tts.py             # ElevenLabs TTS streaming client
├── training/                     # QLoRA fine-tuning pipeline
├── eval/                         # Evaluation harness
├── data/                         # Data processing scripts
└── export/                       # Model export (merge, GGUF)

configs/                          # YAML configuration
├── config.yaml                   # Main pipeline config
├── clinical_triggers.yaml        # Clinical safety phrases
├── handoff_phrases.yaml          # Handoff detection phrases
├── intent_taxonomy.yaml          # Intent classification rules
├── reliability.yaml              # Retry/circuit-breaker tuning
├── auth.yaml                     # Authentication config
└── policies/                     # Policy engine rules

data/knowledge/                   # RAG knowledge base
├── faqs.json                     # FAQ content
├── policies.json                 # Business policies
├── medicine.json                 # Medicine information
└── appointments.json             # Appointment rules

tests/                            # 35 test files
docs/                             # Architecture docs + ADRs
docker/                           # Dockerfile + docker-compose
alembic/                          # Database migrations
```

## Quick Start

### Prerequisites

- Python 3.12+
- (Optional) NVIDIA GPU with CUDA for faster inference
- (Optional) PostgreSQL 16 for production persistence

### Setup

```bash
# 1. Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
.venv\Scripts\Activate.ps1  # Windows PowerShell

# 2. Install dependencies
pip install torch  # Install PyTorch matching your platform first
pip install -r requirements.txt
pip install -r requirements-dev.txt  # test tooling (pytest, pytest-asyncio)

# 3. Configure environment
cp .env.example .env
# Edit .env with your values

# 4. Build RAG index
python src/rag/build_index.py

# 5. Start the API server
python src/api/server.py
```

### Running the Voice Client (TTS)

```bash
# Requires ELEVENLABS_API_KEY in .env
python src/voice/client_tts.py                         # Interactive chat
python src/voice/client_tts.py --message "Hello"       # Single message
```

### Running Tests

```bash
python -m pytest tests/ -v
```

### Docker

```bash
# Training pipeline
docker compose -f docker/docker-compose.yml --profile trainer up --build

# API server
docker compose -f docker/docker-compose.yml --profile api up --build -d

# Dev database (PostgreSQL)
docker compose -f docker/docker-compose.yml --profile dev up -d postgres
```

## Environment Variables

See [`.env.example`](.env.example) for the complete list. Key variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `PORT` | No | API server port (default: 8000) |
| `BASE_MODEL_NAME` | No | HuggingFace model ID (default: Qwen/Qwen2.5-0.5B-Instruct) |
| `AUTH_MODE` | No | `dev` (default) or `production` (OIDC) |
| `PERSISTENCE_MODE` | No | `dev` (default, in-memory) or `production` (PostgreSQL) |
| `DATABASE_URL` | When production | PostgreSQL connection string |
| `ELEVENLABS_API_KEY` | For TTS | ElevenLabs API key |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Liveness check |
| `/ready` | GET | Readiness check (includes DB health in production) |
| `/generate` | POST | Generate a response (supports streaming via NDJSON) |
| `/twiml/inbound-call` | POST/GET | Twilio voice webhook returning TwiML stream connection |
| `/ws/call` | WebSocket | Bidirectional Twilio Media Streams real-time audio pipeline |

### Running the Voice Benchmark Harness

```bash
python src/voice/benchmark.py
```

### POST /generate

```json
{
  "message": "What are your business hours?",
  "history": [],
  "stream": false,
  "session_id": null
}
```

Response:
```json
{
  "response": "Our business hours are Monday through Friday, 9 AM to 5 PM.",
  "is_handoff": false,
  "latency_ms": 450.2
}
```

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — System architecture and design principles
- [`docs/MODULES.md`](docs/MODULES.md) — Module boundaries and interfaces
- [`docs/SECURITY.md`](docs/SECURITY.md) — Security considerations
- [`docs/DATABASE.md`](docs/DATABASE.md) — Database design
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — Technical decision log
- [`docs/adr/`](docs/adr/) — Architecture Decision Records

## License

Private — not for redistribution.
