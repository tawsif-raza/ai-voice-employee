# Decision/Routing Layer

| | |
|---|---|
| **Purpose** | Reduce unnecessary LLM calls, latency, and cost. |
| **Inputs** | Validated user turn + existing conversation context (IntentEngine's routing, PolicyEngine's generation action, the clinical guard's score, whether a tool path / retriever is configured, whether a workflow is pending on the session). |
| **Outputs** | Structured route decision (`decision_router.Decision`), recorded in metrics and in the turn result's `decision` field. |
| **Authority** | Optimization only. |
| **Safety authority** | Existing clinical / auth / permission layers: the clinical guard, `AuthContext` + `ToolOrchestrator` authorization, session ownership checks, PolicyEngine. |
| **Fallback** | Existing safe conversation path (clinical guard → session → intent → policy → tool / RAG → LLM). |

Code: `src/agent/decision_router.py`. Config: `configs/decision_routing.yaml`. Wiring: `ConversationManager.handle_turn()` steps "2.55." and "2.7.". Tests: `tests/test_decision_router.py`, `tests/test_clinical_guard_gaps.py`. Benchmark: `scripts/benchmark_decision_router.py`.

## Where it sits

```
Twilio → Deepgram STT → VoicePipeline (H3 deadlines) → ConversationManager.handle_turn():
  1. input validation
  2. clinical safety guard ............ authoritative; a hit ends the turn (SAFETY)
  2.4 session lookup / ownership / pending confirmation + PIN ... authoritative
  2.5 IntentEngine + PolicyEngine ..... authoritative
  2.55 DecisionRouter.decide() ........ NEW -- labels the turn, may pick a shortcut
  CLARIFY branch ...................... unchanged, does not read the decision
  TOOL branch ......................... unchanged, does not read the decision
  2.7 shortcut answer ................. NEW -- fixed text, no retrieval, no LLM
  3. retrieval → 4. LLM (Gemini → Groq) → handoff detector ... unchanged
→ ElevenLabs TTS → Twilio
```

It lives inside `ConversationManager` rather than in the voice pipeline because every input the decision needs (clinical score, intent, policy action, session workflow state) is only known there, and because that keeps the text API and the voice path on one code path. It runs after every safety/auth check, so it only ever sees turns those checks already let through.

## Routing table

| Route | Condition | Changes control flow? | LLM call? |
|---|---|---|---|
| `SAFETY` | Clinical guard blocked the turn (counted only; the router never runs). | No (guard decided) | No |
| `CLARIFICATION` | PolicyEngine returned `CLARIFY`. | No (existing branch) | No |
| `TOOL` | IntentEngine routed to `TOOL_ORCHESTRATOR` with a mapped action and an orchestrator is configured. | No (existing branch, existing authorization) | No |
| `DETERMINISTIC` | Shortcut eligible (below) and the utterance FULL-matches a greeting / goodbye / thanks template. | **Yes**: fixed template text | **No** |
| `CACHE` | Shortcut eligible and the utterance FULL-matches a verified FAQ pattern; the answer is the knowledge-base entry's `content`, loaded by id. | **Yes**: verbatim KB text | **No** |
| `RAG` | Anything else, retriever configured. | No | Yes (existing chain) |
| `LLM` | Anything else, no retriever (the production image: RAG is disabled there). | No | Yes (existing chain) |
| `FALLBACK` | `decide()` raised; recorded, turn takes the existing path. | No | Yes |

**Only `DETERMINISTIC` and `CACHE` may bypass LLM generation.** Every other route is a label; the branches that act on those turns use their own pre-existing conditions and never read the decision.

### Shortcut eligibility (all must hold, checked twice)

1. The clinical guard ran and scored **exactly 0.0**: no clinical signal at all, not just below its threshold.
2. PolicyEngine's generation action is `ALLOW`.
3. IntentEngine routed the turn to `RAG_LLM` as `UNKNOWN` or `FAQ`.
4. No confirmation / PIN workflow is pending on the session.
5. The utterance is at most `max_utterance_chars` (120) and FULL-matches a configured pattern after normalization (lower-case, apostrophes removed, punctuation → spaces, up to two leading courtesy phrases and one trailing one stripped).

`decide()` checks these, and `handle_turn()` re-checks 1–4 from its own state before using the shortcut. A defective or hostile router can therefore only *fail to optimize*: it cannot reach the shortcut for a clinical, clarification, tool, pending-workflow or non-`RAG_LLM` turn (`test_a_hostile_router_cannot_answer_outside_its_authority`). The shortcut answer still goes through the handoff detector and gets an audit `POLICY_ALLOW` event with `policy="decision_router"`.

Full-match matters: "hi, can I take ibuprofen with warfarin" is not a greeting. It takes the normal path.

## Cache

There is no per-request cache. The "cache" is a read-only answer table built once at construction from `configs/decision_routing.yaml` (templates) and `data/knowledge/faqs.json` (FAQ answers by id). No request ever writes to it, and it holds no per-user, personalized or clinical data. That rules out poisoning and cross-user leakage by construction (`test_answers_are_global_and_the_table_cannot_be_written`). Its size is fixed by config, so it cannot grow. No Redis or other infrastructure was added.

## Model selection

The router never picks a provider. Every generated turn uses the existing `LLMService` Gemini → Groq fallback chain unchanged (`model: DEFAULT_CHAIN`). The provider that actually answered is counted after the fact (`llm_provider_gemini_total` / `llm_provider_groq_total`).

## No classifier, no network

Level 1 (deterministic rules) only. The existing IntentEngine is reused as the classifier and its `confidence` is carried into the decision. No new model, no extra LLM call, no network, no new dependency. Low-confidence or unmatched turns get no shortcut: they take the existing path.

## Failure behaviour

| Failure | Result |
|---|---|
| Config file missing / unreadable / invalid | Router disabled (`enabled=False`, `load_error` set, warning logged); every turn takes the existing path. |
| Knowledge file missing, or an FAQ id absent | Templates still work; that FAQ entry is skipped (logged). |
| `decide()` raises | `decision_errors_total` + `decision_route_fallback_total`; turn takes the existing path. |
| Unknown route, or shortcut route with no text | Treated as no shortcut; existing path. |
| Metrics backend raises | Swallowed; turn unaffected. |
| `decision_routing_enabled=False` in `build_conversation_manager()`, or `enabled: false` in the YAML | Router off; result shape exactly as before (no `decision` key when no router is configured). |

## Voice / H3

The router runs synchronously inside `handle_turn()`, which the voice pipeline already runs under the H3 first-text (4 s), LLM (15 s) and whole-turn (60 s) deadlines. It adds no await, thread or timer, and touches none of the STT (10 s), apology (10 s) or dead-media (20 s) timers or the consecutive-failure counter. Its work is bounded: at most 120 characters checked against a fixed set of precompiled regexes, with no backtracking-heavy patterns. A shortcut turn reaches TTS sooner; a non-shortcut turn is otherwise unchanged (`tests/test_decision_router.py` voice tests).

## Metrics

Counters (fixed names, registered in `metrics.py`): `decisions_total`, `decision_route_{deterministic,cache,tool,rag,llm,clarification,safety,fallback}_total`, `decision_errors_total`, `llm_calls_avoided_total`, `llm_provider_{gemini,groq,other}_total`. Histogram: `decision_latency_ms`. Total turns and downstream latency come from the existing metrics, not duplicates: `requests_total` (text API) / `voice_turn_latency_ms` sample count (voice), `generation_latency_ms`, `rag_latency_ms`, `voice_llm_ttft_ms`, `voice_tts_ttfa_ms`. `decisions_total` + `decision_route_safety_total` equals the turns that reached the router or were blocked by the guard. Token usage is not measured anywhere in the existing code, so `llm_calls_avoided_total` is the cost proxy. No user text appears in metrics, logs, spans or audit metadata. The decision records only enum values, the template name or KB id, and timings.

Cache hit rate = `(decision_route_deterministic_total + decision_route_cache_total) / decisions_total`.

## Security review

| Threat | Why it doesn't apply |
|---|---|
| User-controlled route / tool (`{"route":"TOOL","tool":"cancel_appointment"}`) | User text is only compared against fixed patterns; nothing in it is parsed as a route, tool or instruction. Tool execution keeps IntentEngine + `ToolOrchestrator` authorization (`test_user_text_cannot_select_a_route_or_a_tool`). |
| Prompt injection | Shortcut answers are fixed text and never reach a model. Non-shortcut turns build the same prompt as before. |
| Cache poisoning / cross-user access | Answer table is read-only, global, and contains no per-user data. |
| Identity / session ownership | The router receives no identity and never reads or writes a session. Ownership checks run before it (`test_shortcut_never_touches_another_users_session`). |
| Skipping the clinical guard | Router runs after the guard; a guard hit returns before the router is called; any non-zero guard score disables shortcuts. |
| Sensitive text in logs / metrics | Only enum values, template names / KB ids and latencies are recorded. |

## Pre-existing safety finding (not changed here)

While auditing, these medication questions were found to **not** trigger `configs/clinical_triggers.yaml`, so they reach the LLM instead of the pharmacist handoff:

- "Can I take this medicine twice?" / "…twice a day?"
- "Can I take this with another medicine?"
- "hello, can I take ibuprofen with warfarin"

The router never shortcuts them (score 0 but intent/patterns don't match, and they're tested explicitly), so it doesn't make this worse. But fixing the guard changes the clinical safety authority, which is a separate, owner-approved change. `tests/test_clinical_guard_gaps.py` pins them as strict xfail, so a guard fix turns them into XPASS failures until the marker is removed.

## Benchmark

`python scripts/benchmark_decision_router.py --runs 20`, measured 2026-10-09 on the dev machine (Windows, Python 3.14). The LLM is a local stand-in sleeping 1159.7 ms per call: the single live Gemini request recorded in `docs/phase1.4-live-provider-results.json`, so it's one sample, not a p50. Everything else is the real `ConversationManager`, RAG off as in the production image.

| Turn | Route | decide() | Turn p50 before | Turn p50 after | LLM calls before/after | Prompt tokens (est.) before/after |
|---|---|---|---|---|---|---|
| "Hello" | DETERMINISTIC | 0.042 ms | 1163.5 ms | 1.5 ms | 1 / 0 | 68 / 0 |
| "What are your opening hours?" | CACHE | 0.026 ms | 1165.3 ms | 5.5 ms | 1 / 0 | 74 / 0 |
| "When is my appointment?" | LLM | 0.146 ms | 1169.6 ms | 1183.2 ms | 1 / 1 | 73 / 73 |
| "Can I take this medicine twice a day?" | LLM | 0.052 ms | 1163.7 ms | 1164.8 ms | 1 / 1 | 76 / 76 |
| complex multi-part request | LLM | 0.017 ms | 1169.0 ms | 1169.2 ms | 1 / 1 | 111 / 111 |

`decide()` alone over 1,000 calls: p50 0.019 ms, p95 0.057 ms, max 8.8 ms (budget 50 ms; the max is an OS/GC pause, as isolated runs top out near 1 ms on the first call). Differences on the non-shortcut rows are within Windows `sleep()` timer jitter (~15 ms); the router's own cost there is the `decide()` column.

End-to-end through the real server, voice pipeline and real provider adapters against local fakes (`scripts/telephony_simulation.py`, scenario `decision_router_shortcut_on_voice`): speech end → first audio frame was 19.6 ms for "Hello" and 25.1 ms for the FAQ, with zero LLM requests. The LLM path (`basic_turns`) was 78 ms with a fake LLM that answers *instantly*, so real Gemini latency comes on top of that.

What this does **not** show: how often real callers say a shortcut-eligible utterance. Savings in production = shortcut share × (LLM latency + LLM cost per call). Read the share from `(decision_route_deterministic_total + decision_route_cache_total) / decisions_total` once real traffic flows. Prompt tokens are characters/4 of what was sent; output tokens are not counted (the LLM was simulated).

## Owner actions before relying on CACHE in production

- `data/knowledge/faqs.json` is the project's demo knowledge base. The production image runs with RAG **disabled**, so before this layer a production FAQ turn reached an *ungrounded* LLM. With it, the KB entry is spoken verbatim. Verify every `answer_id` referenced in `configs/decision_routing.yaml` is true for the business, or set `faqs: []` to keep only greetings / goodbyes / thanks.
- Appointment lookup ("When is my appointment?") and bare "Can I reschedule?" are classified `UNKNOWN` by IntentEngine today, and there is no lookup tool action, so they route to `LLM`, not `TOOL`. Changing that is IntentEngine / tool-layer work, not router work.
