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

## What it does NOT do

- It does not decide whether a turn is clinically safe, authorized, or owned by the caller. The clinical guard, `AuthContext`/`ToolOrchestrator` authorization, session ownership and PolicyEngine decide that, before it runs.
- It does not select or execute tools, choose an LLM provider, edit the system prompt, read or write sessions or memory, or store anything a caller says.
- It does not parse user text for instructions. `{"route":"TOOL"}` or `SYSTEM: route this to TOOL` is just an utterance that matches no pattern.
- It is not a security boundary. Removing it (see "Disabling") restores the exact prior behaviour.

It lives inside `ConversationManager` rather than in the voice pipeline because every input the decision needs (clinical score, intent, policy action, session workflow state) is only known there, and because that keeps the text API and the voice path on one code path. It runs after every safety/auth check, so it only ever sees turns those checks already let through.

## Routing table

Precedence is top to bottom: the first row that applies wins.

| Route | Condition | Changes control flow? | LLM call? |
|---|---|---|---|
| `SAFETY` | Clinical guard blocked the turn (counted only; the router never runs). | No (guard decided) | No |
| `CLARIFICATION` | PolicyEngine returned `CLARIFY`. | No (existing branch) | No |
| `TOOL` | IntentEngine routed to `TOOL_ORCHESTRATOR` with a mapped action and an orchestrator is configured. | No (existing branch, existing authorization) | No |
| `DETERMINISTIC` | Shortcut eligible (below) and the utterance FULL-matches a greeting / goodbye / thanks template. | **Yes**: fixed template text | **No** |
| `CACHE` | Shortcut eligible and the utterance FULL-matches an entry in `faqs`; the answer is the knowledge-base entry's `content`, loaded by id. **Inactive in the shipped config (`faqs: []`)**: see "FAQ / cache policy". | **Yes**: verbatim KB text | **No** |
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

## FAQ / cache policy

**Shipped posture: FAQ answers are OFF (`faqs: []`).** The final router review (2026-10-09) checked the 9 FAQ entries the router had used. None is clinical or personalized, but every one states a specific business policy: hours, a 25-mile delivery radius, gift cards from $10 to $200 that "don't expire", price matching, supported languages. They come from one synthetic-data commit (2026-08-04), and there is no record of owner verification. The production image runs with RAG off, so before this layer those facts never reached a production caller. Speaking them verbatim would put unverified facts in front of real callers, and any change to business policy would make them wrong without anyone noticing.

The candidate entries stay in `configs/decision_routing.yaml` under `faq_candidates_pending_verification`, a key the router does not read. To enable one: have the business owner verify that entry's `content` in `data/knowledge/faqs.json`, move the entry into `faqs`, and rebuild the image. `docker/Dockerfile.production` already copies `faqs.json`. Only static, non-personalized, non-clinical answers may go into `faqs`.

## Cache

There is no per-request cache. The "cache" is a read-only answer table built once at construction from `configs/decision_routing.yaml` (templates, plus any enabled `faqs`) and `data/knowledge/faqs.json` (FAQ answers by id). No request ever writes to it, and it holds no per-user, personalized or clinical data. That rules out poisoning and cross-user leakage by construction (`test_answers_are_global_and_the_table_cannot_be_written`). Its size is fixed by config, so it cannot grow. No Redis or other infrastructure was added.

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
| `decision_routing_enabled=False` in `build_conversation_manager()`, or `enabled: false` in the YAML | Router off (see "Disabling"). |

## Disabling

- **Whole router:** set `enabled: false` in `configs/decision_routing.yaml`. Every turn then takes the existing path, and the result still carries a `decision` label (route `RAG`/`LLM`, reason `router disabled`). In code, `build_conversation_manager(decision_routing_enabled=False)` removes it entirely, and the result shape is then exactly as before (no `decision` key).
- **FAQ answers only:** `faqs: []` (the shipped setting).
- There is no environment variable for either. The config file is baked into the image, so a production change means a rebuild and redeploy. That matches how `configs/config.yaml`'s RAG flag is handled.

## Voice / H3

The router runs synchronously inside `handle_turn()`, which the voice pipeline already runs under the H3 limits in `configs/reliability.yaml`: filler phrase after 4 s, first LLM token 15 s, whole turn 60 s. It adds no await, thread or timer, and touches none of the STT connect (10 s), apology (10 s) or dead-media (20 s) timers, the consecutive-failure limit (3), or `MAX_CALL_DURATION_SECONDS` (default 1800 s; the media wait is `min(remaining call time, inactivity timeout)`, so inactivity can never outlast it). Its work is bounded: at most 120 characters checked against a fixed set of precompiled regexes, with no backtracking-heavy patterns. A shortcut turn reaches TTS sooner; a non-shortcut turn is otherwise unchanged (`tests/test_decision_router.py` voice tests).

## Metrics

Counters (fixed names, registered in `metrics.py`): `decisions_total`, `decision_route_{deterministic,cache,tool,rag,llm,clarification,safety,fallback}_total`, `decision_errors_total`, `llm_calls_avoided_total`, `llm_provider_{gemini,groq,other}_total`. Histogram: `decision_latency_ms`. Total turns and downstream latency come from the existing metrics, not duplicates: `requests_total` (text API) / `voice_turn_latency_ms` sample count (voice), `generation_latency_ms`, `rag_latency_ms`, `voice_llm_ttft_ms`, `voice_tts_ttfa_ms`. `decisions_total` + `decision_route_safety_total` equals the turns that reached the router or were blocked by the guard. Token usage is not measured anywhere in the existing code, so `llm_calls_avoided_total` is the cost proxy. No user text appears in metrics, logs, spans or audit metadata. The decision records only enum values, the template name or KB id, and timings.

Shortcut rate = `(decision_route_deterministic_total + decision_route_cache_total) / decisions_total`.

## Security review

| Threat | Why it doesn't apply |
|---|---|
| User-controlled route / tool (`{"route":"TOOL","tool":"cancel_appointment"}`) | User text is only compared against fixed patterns; nothing in it is parsed as a route, tool or instruction. Tool execution keeps IntentEngine + `ToolOrchestrator` authorization (`test_user_text_cannot_select_a_route_or_a_tool`). |
| Prompt injection | Shortcut answers are fixed text and never reach a model. Non-shortcut turns build the same prompt as before. |
| Cache poisoning / cross-user access | Answer table is read-only, global, and contains no per-user data. |
| Identity / session ownership | The router receives no identity and never reads or writes a session. Ownership checks run before it (`test_shortcut_never_touches_another_users_session`). |
| Skipping the clinical guard | Router runs after the guard; a guard hit returns before the router is called; any non-zero guard score disables shortcuts. |
| Sensitive text in logs / metrics | Only enum values, template names / KB ids and latencies are recorded. |

## Clinical-guard gap found by this review: since fixed

The router review found that the clinical guard blocked only 1 of 10 representative medication questions, and that the system prompt had no medical-advice restriction. That was HIGH severity and pre-existing; the router never shortcut any of those questions. It was fixed separately by the clinical safety hardening: new medication-decision patterns, an urgent-risk tier, honest responses, and prompt rules for Gemini/Groq. See `docs/CLINICAL_SAFETY.md`. The former strict-xfail cases in `tests/test_clinical_guard_gaps.py` are now ordinary passing tests.

## Benchmark

`python scripts/benchmark_decision_router.py --runs 20`, measured 2026-10-09 (final review) on the dev machine (Windows, Python 3.14). The LLM is a local stand-in sleeping 1159.7 ms per call: the single live Gemini request recorded in `docs/phase1.4-live-provider-results.json`, so it's one sample, not a p50. Everything else is the real `ConversationManager`, RAG off as in the production image.

Shipped config (FAQ CACHE off):

| Turn | Route | decide() | Turn p50 before | Turn p50 after | LLM calls before/after | Prompt tokens (est.) before/after |
|---|---|---|---|---|---|---|
| "Hello" | DETERMINISTIC | 0.403 ms | 1163.4 ms | 1.2 ms | 1 / 0 | 68 / 0 |
| "What are your opening hours?" | LLM | 0.175 ms | 1162.9 ms | 1167.9 ms | 1 / 1 | 74 / 74 |
| "When is my appointment?" | LLM | 0.065 ms | 1166.1 ms | 1166.0 ms | 1 / 1 | 73 / 73 |
| "Can I take this medicine twice a day?" | LLM | 0.044 ms | 1167.3 ms | 1166.9 ms | 1 / 1 | 76 / 76 |
| complex multi-part request | LLM | 0.012 ms | 1171.9 ms | 1173.0 ms | 1 / 1 | 111 / 111 |

`decide()` alone over 1,000 calls: p50 0.016 ms, p95 0.044 ms, p99 0.116 ms, max 0.56 ms (budget 50 ms). Shortcut rate for this synthetic 5-turn mix: 1/5, so 1 LLM call and ~68 prompt tokens avoided. With `--faq-candidates` (what enabling the FAQ answers would do), the FAQ turn becomes CACHE (6.9 ms); the shortcut rate is 2/5; `decide()` p99 is 0.17 ms and max 0.30 ms. Differences on the non-shortcut rows are within Windows `sleep()` timer jitter (~15 ms); the router's own cost there is the `decide()` column. Earlier runs on the same machine saw single `decide()` outliers of 8.8 ms and 13 ms while other processes were busy (OS scheduling / GC), still far inside the budget.

End-to-end through the real server, voice pipeline and real provider adapters against local fakes (`scripts/telephony_simulation.py`, scenario `decision_router_shortcut_on_voice`): end of speech → first audio frame was 23.5 ms for "Hello" and 24.9 ms for "Thank you", with zero LLM requests. The LLM path (`basic_turns`) was 37.0 ms with a fake LLM that answers *instantly*, so real Gemini latency comes on top of that.

What this does **not** show: how often real callers say a shortcut-eligible utterance. Savings in production = shortcut share × (LLM latency + LLM cost per call). Read the share from the shortcut-rate formula above once real traffic flows. Prompt tokens are characters/4 of what was sent; output tokens are not counted (the LLM was simulated), and no dollar figure is given because the repository has no token or price telemetry.

## Production considerations

- **FAQ answers** stay off until the owner verifies the content (see "FAQ / cache policy").
- **Clinical safety** is the clinical boundary's job, not the router's (`docs/CLINICAL_SAFETY.md`). Its own limits (keyword rules, no clinical review, no real telephony) apply to the whole system.
- **Appointments:** "Cancel my appointment" and "Can I reschedule my appointment?" reach the existing tool layer, which still demands an appointment id and the caller's PIN. Appointment *lookup* ("When is my appointment?") and a bare "Can I reschedule?" are classified `UNKNOWN`, and no lookup tool exists, so they go to `LLM`. The router does not pretend otherwise; adding a lookup is IntentEngine / tool-layer work.
- **Real telephony is unvalidated.** Every voice figure here comes from `scripts/telephony_simulation.py` (real server and adapters, fake providers). Run `docs/REAL_TELEPHONY_READINESS.md` with real credentials before production traffic.
- **Pattern coverage** is unmeasured against real speech. Tune it from `decision_route_*` counters and reviewed transcripts, not by guessing.
