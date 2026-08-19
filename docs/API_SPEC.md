# API Specification

**Status:** Frozen
**Version:** 1.0
**Date:** 2026-08-04
**Architecture Version:** v1.0 (`architecture-v1.0`, commit `a7d567d`)

This document defines the HTTP-level contract layered on top of `MODULES.md` §1 (API Gateway). It does not restate `ARCHITECTURE.md`, `MODULES.md`, `SEQUENCE_DIAGRAMS.md`, `DOMAIN_MODEL.md`, or `docs/adr/ADR-001` through `ADR-006` — every claim below is a pointer into one of those frozen documents, not a re-derivation. Nothing in this document changes architecture, module ownership, or code.

---

## 1. Introduction

This document specifies the HTTP-level contract for the platform's API surface, one layer of detail below `MODULES.md` §1 (API Gateway). It is written for two audiences: engineers implementing against the frozen contract, and reviewers checking a proposed change against Architecture v1.0 before it's built.

Two kinds of content appear here, and Section 9 keeps them structurally separate:
- **Frozen endpoints** (§9.1) — `GET /health` and `POST /chat` — are authorized today by `MODULES.md` v1.0 §1's Public Interfaces (`GET /health`, `POST /generate`). Each carries `Contract Status: Frozen` and has been individually reviewed and validated against the frozen documents.
- **Proposed endpoints** (§16) — not authorized by any frozen document. Each states the specific architectural prerequisite it's blocked on, per the same non-recommending treatment `ADR-006` gives the human-handoff-execution gap.

Every claim in this document is a pointer into `ARCHITECTURE.md`, `MODULES.md`, `SEQUENCE_DIAGRAMS.md`, `DOMAIN_MODEL.md`, or `docs/adr/ADR-001` through `ADR-006` — none of it is restated. Where this document proposes something those sources leave unspecified (a header name, a status code mapping, a field name), it is flagged inline as this document's own elaboration, not a frozen requirement.

## 2. API Design Principles

Every principle below is derived from a frozen source, not invented for this document:

- **Thin transport layer.** API Gateway "carries no business or safety logic itself" (`MODULES.md` §1 Purpose) — this document never assigns business logic to an endpoint; every decision belongs to the module named in that endpoint's Owner Module / delegation.
- **Single mediated entry point.** Everything routes through Conversation Manager; API Gateway has "no direct dependency on Intent Engine, Safety Engine, RAG Engine, LLM Service, or Tool Orchestrator" (§1 Dependencies; Dependency Rules 2–3). This is the organizing constraint behind the Frozen/Proposed split in Section 9 and Section 16.
- **Fail-safe, not silent.** Per `ARCHITECTURE.md` Core Principle 5 and §1/§2 Error Handling, a downstream failure produces a documented fallback response, never a raw exception or hung connection. Section 5 formalizes the resulting error envelope; note that most "failures" in `MODULES.md` are represented as a successful response with degraded content, not as an error at all (see Section 5).
- **Streaming-first where a module defines it.** §1, §2, and §7 define streaming variants; Section 6 formalizes the wire protocol once, rather than per endpoint.
- **Stateless-by-default at the transport level.** The caller supplies full history per request (`ARCHITECTURE.md` §8) until Memory (§4) is implemented — reflected in `POST /chat`'s `history` field being required input, not server-side state.
- **No new module dependency is introduced by adding an endpoint.** An endpoint that would require API Gateway to depend on a module beyond Conversation Manager is, by definition, not authorized by `MODULES.md` v1.0 — it belongs in Section 16, not Section 9, regardless of how useful it would be.

## 3. Authentication & Authorization

**No authentication or authorization exists in Architecture v1.0.** This is a documented, accepted gap, not an omission of this document:

- `MODULES.md` §1 Responsibilities: API Gateway is to "own transport-level concerns as they're added (authentication, rate limiting) — currently absent, tracked as a known gap."
- `ARCHITECTURE.md` §9 (Security Considerations) names the same absence at the system level; `ARCHITECTURE_REVIEW.md` finding 5.2 references it as existing context for its own rate-limiting finding.

Both frozen endpoints (§9.1.1, §9.1.2) state `Authentication: None` accordingly. This section makes that gap's implication explicit for the API layer as a whole: **every endpoint in Section 9 is reachable by any client that can reach API Gateway over the network**, with no identity, scoping, or access control of any kind.

**Deferred to Architecture v1.1:** the authentication/authorization mechanism itself is not designed here. One forward note, not a decision: conventionally, liveness endpoints (`GET /health`) remain unauthenticated even after an authN scheme is introduced elsewhere, so infrastructure probes keep working — this document flags that convention for v1.1 to consider, without adopting it as a requirement.

## 4. Versioning Strategy

**Current state:** paths are unversioned today — `MODULES.md` §1's Public Interfaces are literally `GET /health` and `POST /generate`, with no `/v1/` prefix or version header. This document does not change that; `GET /health` and `POST /chat` are documented at their frozen, unversioned paths.

**Proposed convention for future evolution:** this document extends `DOMAIN_MODEL.md`'s Global Versioning Convention to the API layer — additive changes (a new optional field) don't require a version marker; a breaking change (removing/repurposing a field, changing a required field's type or an enum's meaning) requires an explicit new version, surfaced as a path prefix (e.g. `/v2/chat`) once the platform has more than one concurrently-supported contract version. No such breaking change exists yet, so this remains a stated convention, not an active requirement.

This is a documentation-level HTTP convention, not an architecture change — it doesn't alter any module boundary, ownership, or dependency.

## 5. Error Handling Standard

**Two distinct categories, not one — this distinction governs every Error Codes table in Section 9:**

1. **Fail-safe degraded responses** — per `ARCHITECTURE.md` Core Principle 5 and `MODULES.md` §1/§2 Error Handling, most documented failures (a clinical trigger, an LLM Service failure, a RAG Engine failure) are *not* errors from the API's perspective. They are `200 OK` responses using the endpoint's normal Response schema, with fields signaling the degraded state (`is_handoff`, `clinical_guard_triggered`, an ungrounded response with no `retrieved_chunks`). See `SEQUENCE_DIAGRAMS.md` Diagram 6 for the authoritative catalog of which failure maps to which fallback.
2. **True errors** — a request that cannot be fulfilled at all (malformed input, the service unreachable, a timeout that abandons the turn). These use the standard error envelope below and an appropriate 4xx/5xx status.

**Standard error envelope** (this document's own proposal — no envelope shape is specified in `MODULES.md`, only the principle that "no internal state, prompts, or stack traces are exposed to the client," §1 Error Handling):

```json
{
  "error": {
    "code": "string — machine-readable, e.g. \"malformed_request\", \"service_unavailable\", \"request_timeout\"",
    "message": "string — human-readable, safe to display, never a raw exception or stack trace",
    "request_id": "string — echoes X-Request-ID (§7), for correlating with Analytics/logs"
  }
}
```

`POST /chat`'s `400`/`503`/`504`/`5xx` rows (§9.1.2) are instances of this envelope. `GET /health` has no error path beyond process-level failure (§9.1.1) and doesn't use this envelope in practice.

## 6. Streaming Protocol

Formalizes, once, the wire framing `POST /chat`'s Streaming Behavior (§9.1.2) already uses — built directly on `DOMAIN_MODEL.md`'s `StreamingChunk` entity, not a new contract.

**Transport:** `Content-Type: application/x-ndjson` — newline-delimited JSON, one `StreamingChunk`-shaped object per line, flushed as each chunk becomes available (no buffering the full response before the first line is sent).

**Framing rules** (from `DOMAIN_MODEL.md` `StreamingChunk` State Invariants):
- All lines in one response share a single `stream_id`.
- `sequence` starts at 0 and is contiguous — no gaps, no duplicates.
- Every line but the last carries a `text` field (for `POST /chat`) with `is_final: false`.
- Exactly one terminal line carries `is_final: true` and a `final_payload` — the same object shape as that endpoint's non-streaming Response.

**Scope note:** this section covers the API Gateway↔Client boundary only. Voice TTS's `synthesize_stream` (§11) uses the same `StreamingChunk` envelope conceptually but is a client-side adapter that never calls API Gateway (`SEQUENCE_DIAGRAMS.md` Diagram 2) — it is out of scope for this HTTP protocol section and isn't reachable through any endpoint in Section 9.

**Client behavior on a mid-stream failure:** not specified anywhere in the frozen documents (`SEQUENCE_DIAGRAMS.md` Inputs for Version 1.1, item M14) — deferred to Architecture v1.1, not resolved here.

## 7. Common Headers

Formalizes the headers already used ad hoc across §9.1.1/§9.1.2 into one canonical table:

| Header | Direction | Applies to | Notes |
|---|---|---|---|
| `Content-Type: application/json` | Request | Endpoints with a body (`POST /chat`) | Required |
| `Content-Type` | Response | All | `application/json` (default) or `application/x-ndjson` (streaming responses, §6) |
| `X-Request-ID` | Request/Response | Endpoints that invoke Conversation Manager (`POST /chat`) | Correlation ID API Gateway "attaches/propagates for Analytics" (`MODULES.md` §1 Responsibilities). The header name itself is this document's proposal — no literal name is specified in `MODULES.md` |

**Not used:** `Accept`-based content negotiation (streaming is selected via the `stream` body field, not a header) and any authentication header (Section 3 — none exists in v1.0).

## 8. API Conventions

Cross-cutting rules every endpoint spec in Section 9 assumes rather than restates:

- **Field naming:** `snake_case`, matching `MODULES.md`'s interface signatures and `DOMAIN_MODEL.md`'s field tables verbatim (e.g. `session_id`, `handoff_confidence`).
- **Timestamps:** ISO 8601, matching `DOMAIN_MODEL.md`'s `timestamp` type wherever a `timestamp`-typed field is wire-exposed.
- **IDs:** opaque strings (`session_id`, `request_id`) — no format is mandated by `MODULES.md`; callers must not parse or assume structure.
- **Null vs. omitted:** an optional field that doesn't apply is **omitted**, not sent as `null` — matches `DOMAIN_MODEL.md`'s convention for `Turn`'s optional fields ("omitted, not null, when the producing module hasn't run") and is applied at the API layer consistently (e.g. `POST /chat`'s `intent` field, §9.1.2).
- **HTTP status codes:** `2xx` for every fail-safe fallback that completes the turn with substitute content (Section 5); `4xx` for a malformed caller request; `5xx` for a genuine service-side failure. No endpoint in this document uses a `3xx` redirect.
- **Content negotiation:** `Content-Type` alone determines request/response format; streaming is a body-level flag (`stream`), not header-level negotiation (Section 7).

## 9. Endpoint Specifications

### 9.1 Frozen (Authorized by `MODULES.md` v1.0 §1)

#### 9.1.1 `GET /health`

**Contract Status:** Frozen — Authorized by `MODULES.md` v1.0 §1.

**Purpose**
Liveness/readiness check for API Gateway — per §1 Public Interfaces: "`GET /health` → liveness/readiness status." Confirmed in `ARCHITECTURE.md` (API Layer row: "Implemented (FastAPI: `/health`, `/generate`)").

**Owner Module**
API Gateway (§1).

**Related Sequence Diagram**
None. None of `SEQUENCE_DIAGRAMS.md`'s six diagrams model a health-check flow — all six are turn-oriented.

**Related Domain Entities**
None. `DOMAIN_MODEL.md` doesn't model health/liveness status as a domain entity — it's operational/infrastructure state, not a conversational concept. The response shape below is this document's own minimal proposal.

**Authentication**
None — same v1.0 gap as `POST /chat` (§3). Forward note for Section 3: health-check endpoints are conventionally left unauthenticated even after authN is introduced elsewhere, so infra probes can still reach them — not decided here.

**Headers**

| Header | Direction | Notes |
|---|---|---|
| — | Request | None required |
| `Content-Type: application/json` | Response | |

No `X-Request-ID` — §9 scopes Analytics/correlation to conversation turns; a health probe isn't a turn.

**Request**
No body, no query parameters, no path parameters.

**Response**

Note: §1's phrase is "liveness **and** readiness status" from one endpoint — `MODULES.md` doesn't distinguish them as separate signals or endpoints, and neither does `ARCHITECTURE.md`. The response below follows that single-endpoint, single-payload design.

| Field | Type | Description |
|---|---|---|
| `status` | string | `"ok"` — no enum is specified in `MODULES.md`; this document proposes `"ok"` as the only value needed for v1.0, for continuity with existing usage |
| `model_loaded` | boolean | Readiness signal — see Architecture Note below |

**Architecture Note (deferred to Architecture v1.1):** §1 Dependencies states API Gateway's only dependency is "Conversation Manager only... everything is mediated through the Conversation Manager." `model_loaded` is knowledge that originates in LLM Service (§7), not Conversation Manager or API Gateway. Read strictly, API Gateway cannot query LLM Service directly to populate this field — it would need Conversation Manager to expose an aggregate readiness signal for API Gateway to relay. Today this doesn't manifest as a real cross-module call because Conversation Manager and LLM Service are still the same fused process (`MODULES.md` "Known Implementation Debt"). This is a known consideration for Architecture v1.1, once Conversation Manager and LLM Service are physically separated (see [ADR-001](adr/ADR-001-conversation-manager.md) Future Work) — not resolved here, and no ownership change is made by this document.

**Streaming Behavior**
Not applicable — synchronous request/response only, no streaming interface defined or implied by §1 for this endpoint.

**Error Codes**

| Status | Condition | Source |
|---|---|---|
| `200 OK` | API Gateway process is responding — the entire point of a liveness endpoint | §1 |

No other status is specified in `MODULES.md`; if the process cannot respond at all, no HTTP status is returned regardless.

**Timeout Behavior**
Not applicable in the sense `POST /chat` has one — this endpoint doesn't invoke the `turn()` call chain, so §1's request-timeout behavior (which bounds a Conversation Manager call) doesn't apply here.

**Retry Semantics**
None needed server-side — naturally idempotent and side-effect-free; client-side polling (e.g. load-balancer health probes) is the expected caller pattern, not a server-defined retry contract.

**Rate Limits**
None in v1.0 — same documented gap as `POST /chat` (§12, `ARCHITECTURE_REVIEW.md` 5.2). Forward note: health-check endpoints are conventionally exempted from rate limiting even once it's introduced elsewhere — not decided here.

**Observability**
None. This endpoint doesn't produce a `Turn` or invoke Conversation Manager, so no `AnalyticsEvent` is emitted — §9 scopes Analytics to per-turn events only.

**Security Notes**
No authentication (see above). Unlike `POST /chat`, this endpoint carries no user content, so `ARCHITECTURE_REVIEW.md` 5.1 (prompt injection) and 5.4 (PII exposure) don't apply. `model_loaded` is minor operational-state disclosure — low severity, noted for completeness.

**Related ADRs**
[ADR-001](adr/ADR-001-conversation-manager.md) — only via the `model_loaded` Architecture Note above. No other ADR addresses this endpoint.

#### 9.1.2 `POST /chat`

**Contract Status:** Frozen — Authorized by `MODULES.md` v1.0 §1 (`POST /generate`).

**Purpose**
Executes one conversation turn: accepts a user message and prior history, orchestrates intent classification, safety checks, retrieval, and generation as needed, and returns the assistant's response. Matches `MODULES.md` §1's `POST /generate` and delegates to §2's `ConversationManager.turn(...)`.

**Owner Module**
API Gateway (§1) — transport marshaling only, per §1 Purpose: "carries no business or safety logic itself." All orchestration is delegated to and owned by Conversation Manager (§2).

**Related Sequence Diagram**
`SEQUENCE_DIAGRAMS.md` Diagram 1 (Chat Request Flow) — primary. Diagram 6 (Error Recovery Flow) governs every failure branch below. Diagram 5 (Human Handoff Flow) governs the `is_handoff` branch specifically.

**Related Domain Entities**
`DOMAIN_MODEL.md` — `Message`, `Turn` (wire-visible, request/response); `PromptBundle`, `ContextWindow`, `IntentResult`, `SafetyResult`, `RetrievalResult` (internal to turn processing, flattened into the response body, not returned as separate objects); `StreamingChunk` (wire-visible, streaming mode only); `AnalyticsEvent` (emitted as a side effect — see Observability).

**Authentication**
None in Architecture v1.0. See §3 (Authentication & Authorization) — a documented, accepted gap per `MODULES.md` §1 Responsibilities and `ARCHITECTURE.md` §9. Not resolved by this endpoint.

**Headers**

| Header | Direction | Notes |
|---|---|---|
| `Content-Type: application/json` | Request | Required |
| — | Request | Streaming is selected via the `stream` body field, not content negotiation — `MODULES.md` doesn't define an `Accept`-based mechanism |
| `Content-Type` | Response | `application/json` (non-streaming) or `application/x-ndjson` (streaming), per §6 |
| `X-Request-ID` | Request/Response | Correlation ID API Gateway "attaches/propagates for Analytics" (§1 Responsibilities). No literal header name is specified in `MODULES.md` — `X-Request-ID` is proposed here as the canonical convention Section 7 will also define, not a frozen requirement |

**Request**

⚠️ **Note on `history`'s shape:** `MODULES.md` §2 types `turn()`'s history parameter as `list[Turn]`. Per `DOMAIN_MODEL.md`'s `Turn` entity, a `Turn` requires `turn_index`, `user_message`, **and** `assistant_message` together — i.e. each history entry is a paired exchange, not a single flat role/content record. This document follows that frozen shape. It differs from today's actual code (`src/api/server.py`'s `ChatRequest.history: list[dict]`, flat role/content) — that gap belongs to `MODULES.md`'s "Known Implementation Debt," not to this document.

| Field | Type | Required | Description |
|---|---|---|---|
| `message` | string | Yes | The current user turn |
| `history` | `Turn[]` | No (default `[]`) | Prior turns, each a paired user+assistant exchange — see note above |
| `session_id` | string | No | Per §2's `Optional[str]`; advisory only today since the platform is stateless by design (`ARCHITECTURE.md` §8) until Memory (§4) is implemented |
| `stream` | boolean | No (default `false`) | Selects streaming vs. blocking response mode |

**Response**

Non-streaming (`stream: false`) — a flattened `TurnResult`, matching §2 Outputs verbatim ("response text, handoff flag/confidence, retrieved chunks, clinical-guard status, classified intent, latency"):

| Field | Type | Always present? | Source |
|---|---|---|---|
| `response` | string | Yes | Assistant `Message.content` |
| `is_handoff` | boolean | Yes | Post-generation `SafetyResult.triggered` (§5) |
| `handoff_confidence` | float | Yes | Post-generation `SafetyResult.confidence` |
| `clinical_guard_triggered` | boolean | Yes | Pre-generation `SafetyResult.triggered` (§5) |
| `retrieved_chunks` | `RetrievalResult[]` | Only if RAG Engine was invoked | §6 — field name chosen for continuity with existing usage; §1 Outputs uses the phrase "retrieved sources," not a literal field name |
| `intent` | `IntentResult` | Only once Intent Engine (§3) is implemented | §3 Status — omitted, not null, today |
| `latency_ms` | float | Yes | End-to-end turn latency |

**Streaming Behavior**
When `stream: true`: NDJSON per §6, one `StreamingChunk`-shaped line per token (`sequence` incrementing from 0, `text` field populated), terminating in exactly one line with `is_final: true` whose `final_payload` carries the same object shape as the non-streaming Response above (`DOMAIN_MODEL.md` `StreamingChunk`). Per `SEQUENCE_DIAGRAMS.md` Diagram 1: streaming begins only after the pre-generation clinical check clears — a clinical trigger short-circuits before any token is streamed — and ends once the post-generation handoff check completes.

**Error Codes**

| Status | Condition | Source |
|---|---|---|
| `200 OK` | Normal completion, **and** every fallback that completes the turn with substitute content: clinical short-circuit, LLM failure → apology-and-handoff, RAG failure → ungrounded | §1/§2 — "never a raw exception... never hanging"; these are valid completed turns with a body signaling degraded state |
| `400 Bad Request` | Malformed request, rejected before reaching Conversation Manager | §1 Error Handling |
| `503 Service Unavailable` | Conversation Manager unreachable (e.g. not yet initialized) | §1 |
| `504 Gateway Timeout` | API Gateway's request timeout is exceeded — the turn is abandoned, not completed with fallback content | §1 Error Handling; `SEQUENCE_DIAGRAMS.md` Diagram 1 Failure Modes ("turn abandoned") — HTTP status not specified in `MODULES.md`; `504` is this document's proposed mapping, `503` is an equally defensible alternative `MODULES.md` doesn't adjudicate between |
| `5xx` (generic) | Any other Conversation Manager failure not mapped above | §1: "mapped to a generic error response — no internal state, prompts, or stack traces are exposed to the client" |

**Timeout Behavior**
API Gateway enforces a request timeout around the `turn()` call (§1 Error Handling; `SEQUENCE_DIAGRAMS.md` Diagram 1 Note). On timeout, the turn is abandoned and a `504` "try again" response is returned rather than the connection hanging. The exact timeout duration is **not specified** in `MODULES.md` v1.0 — flagged as open, not invented here.

**Retry Semantics**
None server-side. Per `ARCHITECTURE.md` Core Principle 5 and `SEQUENCE_DIAGRAMS.md`'s Invariants throughout, the platform substitutes a documented fallback for a retry at this layer (`SEQUENCE_DIAGRAMS.md` Diagram 1 Failure Modes: "No retry specified" for the timeout case specifically). Retrying is a caller/client decision, outside this contract.

**Rate Limits**
None in v1.0. See §12 — documented gap (`ARCHITECTURE_REVIEW.md` 5.2).

**Observability**
One `AnalyticsEvent` per turn (`DOMAIN_MODEL.md`), emitted by Conversation Manager (§2 Responsibilities: "Emit turn events to Analytics") after the response is determined — asynchronous, non-blocking, never affects this endpoint's response (§9: "must never be something the Conversation Manager blocks on").

**Security Notes**
No authentication exists (see above). This endpoint is the primary carrier of raw user text content — `ARCHITECTURE_REVIEW.md` 5.1 (prompt-injection risk, unaddressed) and 5.4 (conversation-content logging/PII exposure risk, unaddressed) both apply directly here and are not resolved by this document.

**Related ADRs**
[ADR-001](adr/ADR-001-conversation-manager.md) (orchestration this endpoint delegates to), [ADR-005](adr/ADR-005-safety-pipeline.md) (the two safety checks embedded in this flow), [ADR-002](adr/ADR-002-rag-architecture.md) (optional retrieval step), [ADR-004](adr/ADR-004-memory-strategy.md) (governs `history`'s future evolution once Memory exists), [ADR-003](adr/ADR-003-tool-orchestration.md) (the future business-action branch §2 Responsibilities names within this same flow), [ADR-006](adr/ADR-006-human-handoff-gap.md) (the `is_handoff` flag this endpoint surfaces has no defined execution path beyond it).

### 9.2 Proposed (see Section 16)

Not yet drafted — deferred to Section 16 (Future API Extensions).

## 10. Request Models

An index, not a duplicate — full field contracts stay in `DOMAIN_MODEL.md`; this table maps entity → where it appears on the wire at this API boundary.

| Entity | `DOMAIN_MODEL.md` Reference | Used In | Wire Notes |
|---|---|---|---|
| `Turn` | Group 1 | `POST /chat` request `history` array | Each entry is a paired user+assistant exchange, not a flat message — see §9.1.2's Request note |
| `Message` | Group 1 | Nested within each `Turn` in `history` (`user_message`, `assistant_message`) | `{role, content}` minimum shape |

No other `DOMAIN_MODEL.md` entity is directly wire-visible in a request body for the two frozen endpoints. `GET /health` has no request body.

## 11. Response Models

Same treatment for response bodies. Note that `POST /chat`'s response is a **flattened composition**, not one `DOMAIN_MODEL.md` entity verbatim — it combines fields from several internal entities into the single `TurnResult`-shaped body `MODULES.md` §2 Outputs describes.

| Entity / Field group | `DOMAIN_MODEL.md` Reference | Used In | Wire Notes |
|---|---|---|---|
| Assistant `Message.content` | Group 1 | `POST /chat` response `response` field | Flattened to a bare string, not a `{role, content}` object |
| `SafetyResult` (pre- and post-generation) | Group 2 | `POST /chat` response `clinical_guard_triggered`, `is_handoff`, `handoff_confidence` | Two `SafetyResult` instances flattened into three top-level fields, not returned as an array |
| `RetrievalResult[]` | Group 3 | `POST /chat` response `retrieved_chunks` | Returned as-is (array), only when RAG Engine was invoked |
| `IntentResult` | Group 2 | `POST /chat` response `intent` | Returned as-is; omitted today since Intent Engine (§3) isn't implemented |
| `StreamingChunk` | Group 5 | `POST /chat` streaming response lines | Returned as-is, per Section 6 |
| — (no `DOMAIN_MODEL.md` source) | — | `GET /health` response (`status`, `model_loaded`) | This document's own minimal proposal — health/liveness isn't modeled as a domain entity (§9.1.1) |

## 12. Rate Limiting

**No rate limiting exists in Architecture v1.0.** Per `MODULES.md` §1 Responsibilities (rate limiting listed alongside authentication as "currently absent, tracked as a known gap") and `ARCHITECTURE_REVIEW.md` finding 5.2 ("No rate limiting / resource-exhaustion consideration"). Both frozen endpoints (§9.1.1, §9.1.2) state `Rate Limits: None` accordingly.

This is a documented gap, not a policy this document invents. **Deferred to Architecture v1.1.**

## 13. Idempotency

| Endpoint | Idempotent? | Basis |
|---|---|---|
| `GET /health` | Yes, naturally | Read-only, side-effect-free |
| `POST /chat` | No, by design | Each call produces a new `Turn` (`DOMAIN_MODEL.md` — `turn_index` strictly increasing, never reused) |

No idempotency-key mechanism (e.g. a client-supplied key to safely retry a `POST /chat` call without double-producing a turn) exists anywhere in `MODULES.md` v1.0. **Deferred to Architecture v1.1** — not resolved by this document.

## 14. Observability

**Correlation:** API Gateway attaches/propagates the `X-Request-ID` header (§7) for every request; `MODULES.md` §1 Responsibilities ties this specifically to Analytics correlation.

**Per-turn events:** `POST /chat` emits one `AnalyticsEvent` per turn (`DOMAIN_MODEL.md` Group 6), produced by Conversation Manager (§2 Responsibilities: "Emit turn events to Analytics") — asynchronous, non-blocking, and never able to affect the response already returned to the caller (§9: "must never be something the Conversation Manager blocks on"). `GET /health` produces no such event — it isn't a turn (§9.1.1).

**Compliance view:** `DOMAIN_MODEL.md`'s `AuditLog` entity (Group 6) captures safety decisions and tool-execution outcomes durably, distinct from `AnalyticsEvent`'s metrics purpose — also internal-only today.

**No external observability surface exists.** Both `AnalyticsEvent` and `AuditLog` are internal to Analytics (§9); there is no authorized endpoint to query them (`GET /metrics`, Section 16, is proposed but not authorized).

## 15. Security Considerations

Rolls up the API-relevant subset of `ARCHITECTURE.md` §9 and `ARCHITECTURE_REVIEW.md`'s security findings (5.1–5.6) by reference — not restated here.

| Finding | Applies to this API surface? | Where noted |
|---|---|---|
| No authN/authZ (`ARCHITECTURE.md` §9) | Yes — every endpoint in Section 9 | Section 3 |
| 5.1 Prompt injection from user input | Yes — `POST /chat` is the primary carrier of raw user text | §9.1.2 Security Notes |
| 5.2 No rate limiting | Yes — all endpoints | Section 12 |
| 5.3 Knowledge base integrity/access control | No — a RAG Engine content-authoring concern, not an API-layer one | Out of scope for this document |
| 5.4 Conversation-content logging/PII exposure | Yes — `POST /chat` request/response content, and its `AnalyticsEvent` | §9.1.2 Security Notes; Section 14 |
| 5.5 Model/dependency supply-chain risk | No — not an API-layer concern | Out of scope for this document |
| 5.6 Future Business Action Layer security model | Not yet — relevant once `POST /tool` (Section 16) is authorized | Section 16 |

None of these are resolved by this document. The single largest current exposure for this API surface specifically is the combination of no authentication (Section 3) with no rate limiting (Section 12) on an endpoint that accepts arbitrary user text (`POST /chat`).

## 16. Future API Extensions

None of the six endpoints below are authorized by `MODULES.md` v1.0. Each entry states the specific architectural prerequisite it's blocked on — this document doesn't select, recommend, or design a resolution for any of them, the same non-recommending treatment `ADR-006` gives the human-handoff-execution gap.

**`POST /voice`**
Conceptual purpose: submit captured audio and receive synthesized audio back in one call.
Architectural Prerequisite: conflicts with `SEQUENCE_DIAGRAMS.md` Diagram 2's design, where the Client — not API Gateway — sequences Voice STT → `POST /chat` → Voice TTS. `MODULES.md` §10/§11 state Voice STT/TTS "does not call API Gateway itself." Authorizing this endpoint would mean either moving that orchestration server-side (an architecture change) or making this a thin alias with no new capability.

**`POST /tool`**
Conceptual purpose: let a client trigger a business action directly.
Architectural Prerequisite: violates `MODULES.md` §8 Ownership ("Conversation Manager is the only module allowed to call `Tool Orchestrator.invoke()`") and Dependency Rule 3 (single-hub). Would require API Gateway to gain a new direct dependency on Tool Orchestrator, bypassing Conversation Manager's exclusive decision-making role (`ADR-003`).

**`POST /knowledge/upload`**
Conceptual purpose: add or update knowledge base content via API.
Architectural Prerequisite: RAG Engine (§6) has no write/upload interface — only `retrieve()` and `rebuild_index()`. `DOMAIN_MODEL.md`'s `KnowledgeDocument` is explicitly authored offline, outside the request path. Would require both a new RAG Engine interface and a new API Gateway→RAG Engine dependency edge, which §1 Dependencies currently forbids (`ADR-002`).

**`GET /metrics`**
Conceptual purpose: expose Analytics data externally.
Architectural Prerequisite: Analytics (§9) has no public HTTP interface — only `record_turn()`/`run_benchmark()`, invoked internally by Conversation Manager. Would require a new API Gateway→Analytics dependency edge.

**`GET /sessions`**
Conceptual purpose: list active/past conversation sessions.
Architectural Prerequisite: Memory (§4) has no "list all sessions" capability — only per-`session_id` `retrieve()`. Would require both a new Memory interface and a new API Gateway→Memory dependency edge (`ADR-004`).

**`DELETE /session`**
Conceptual purpose: clear a specific conversation's stored state.
Architectural Prerequisite: Memory's `clear(session_id)` already exists in the frozen interface (§4) — closer to authorized than the others — but exposing it directly from API Gateway still requires the same new API Gateway→Memory dependency edge as `GET /sessions` (`ADR-004`).

All six are deferred to Architecture v1.1 for an ownership/authorization decision.

## 17. Appendix — Traceability Matrix

| Endpoint | Owner Module | `MODULES.md` § | Related Sequence Diagram | Related Domain Entities | Related ADR(s) | Status |
|---|---|---|---|---|---|---|
| `GET /health` | API Gateway | §1 | None | None (endpoint-local shape) | ADR-001 (tangential) | **Frozen** |
| `POST /chat` | API Gateway → Conversation Manager | §1, §2 | Diagram 1, 5, 6 | `Message`, `Turn`, `PromptBundle`, `ContextWindow`, `IntentResult`, `SafetyResult`, `RetrievalResult`, `StreamingChunk`, `AnalyticsEvent` | ADR-001, ADR-002, ADR-003, ADR-004, ADR-005, ADR-006 | **Frozen** |
| `POST /voice` | *(would be API Gateway / Client-orchestrated)* | §10, §11 | Diagram 2 | `VoiceRequest`, `VoiceResponse`, `Message` | ADR-001 | **Proposed** — not authorized |
| `POST /tool` | *(would be API Gateway → Tool Orchestrator)* | §8 | Diagram 3 | `ToolRequest`, `ToolResponse`, `ToolExecution` | ADR-003 | **Proposed** — not authorized |
| `POST /knowledge/upload` | *(would be API Gateway → RAG Engine)* | §6 | Diagram 4 | `KnowledgeDocument` | ADR-002 | **Proposed** — not authorized |
| `GET /metrics` | *(would be API Gateway → Analytics)* | §9 | None | `AnalyticsEvent`, `BenchmarkReport`, `AuditLog` | — | **Proposed** — not authorized |
| `GET /sessions` | *(would be API Gateway → Memory)* | §4 | None | `ConversationSession` | ADR-004 | **Proposed** — not authorized |
| `DELETE /session` | *(would be API Gateway → Memory)* | §4 | None | `ConversationSession` | ADR-004 | **Proposed** — not authorized |
