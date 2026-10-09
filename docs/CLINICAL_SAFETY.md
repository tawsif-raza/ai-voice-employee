# Clinical Safety Boundary

| | |
|---|---|
| **Purpose** | Keep medication-decision and possible-emergency requests from receiving independent LLM advice. |
| **Where** | `ConversationManager.handle_turn()` step 2, before session, intent, policy, Decision Router, tools, retrieval and the LLM. |
| **Engine** | `HandoffDetector` (`src/inference/handoff_detector.py`), unchanged. Two configurations of it (ADR-005: one engine, several configurations). |
| **Authority** | The one authoritative clinical-safety boundary. The Decision Router, IntentEngine and the LLM prompt never override it. |
| **What it is not** | Clinical validation. These are deterministic keyword / pattern rules tested in software. They reduce risk; they cannot guarantee that every clinical question is caught. |

Tests: `tests/test_clinical_safety_hardening.py`, `tests/test_clinical_guard_gaps.py`, `tests/test_clinical_guard.py`, plus `test_clinical_safety_holds_under_the_production_posture` in `tests/test_production_posture.py`. Voice: the `urgent` and `clinical` scenarios in `scripts/telephony_simulation.py`.

## Precedence

```
1. input validation (empty / non-string)                       unchanged
2. clinical safety step
   a. urgent tier   configs/urgent_triggers.yaml   -> URGENT_SAFETY_RESPONSE, turn ends
   b. clinical tier configs/clinical_triggers.yaml -> CLINICAL_HANDOFF_RESPONSE, turn ends
2.4 session lookup / ownership / pending confirmation + PIN    unchanged (H2)
2.5 IntentEngine + PolicyEngine                                unchanged
2.55 Decision Router (optimization only)                       unchanged; any partial score from either tier disables its shortcuts
tool / RAG / LLM (Gemini -> Groq)                              unchanged
```

A blocked turn returns before anything else runs: no session read, no tool, no retrieval, no router call, and no provider call (Gemini *or* the Groq fallback). Matching is on the whole utterance, so a greeting or FAQ wording can't hide a clinical question ("Hello, should I double my dose?" is blocked).

Authentication isn't needed to receive a safety response. Both responses are generic and contain no account data, so they run before the H2 identity checks, which are themselves unchanged.

## Categories

| Category | Examples | Response | Config |
|---|---|---|---|
| **Urgent risk** | trouble breathing, throat / lips / face swelling, passed out, unresponsive, seizure, chest pain, "I took too many / a whole bottle / more than prescribed", a child swallowed pills | `URGENT_SAFETY_RESPONSE`: call the local emergency number now (or poison control for a possible overdose); "I can't call anyone for you from this line." | `configs/urgent_triggers.yaml` |
| **Medication / treatment decision** | dose safety, increase / decrease / double / skip / stop / start, frequency and timing, quantity ("two tablets", "400 mg"), combining with another medicine or alcohol, suitability for a person; typos (`medecine`, `dosege`) and statement forms ("how much ibuprofen I can take") | `CLINICAL_HANDOFF_RESPONSE`: can't advise on medicines or doses; ask your pharmacist or doctor directly; can't transfer from this call; if unwell, call the local emergency number. | `configs/clinical_triggers.yaml` |
| **Administrative** | hours, location, delivery, orders, prescription pickup / refill, appointment admin, greetings | Existing routes (Decision Router, tools, LLM). | — |
| **Ambiguous** | "can I take it again?", "should I keep taking it?", "can I take two?" | Treated as a medication decision (conservative): the clinical response, not an LLM answer. | `configs/clinical_triggers.yaml` |

Urgent patterns are written as *events* ("I took too many"), not hypotheticals ("can I overdose on …"), so ordinary medication questions get the pharmacist/doctor response rather than an emergency instruction. Urgent outranks clinical when both match.

## Responses: what they do not claim

- **No transfer.** No transfer mechanism exists (ADR-006: `is_handoff` only flags the turn; the voice path does not act on it). The old response said "let me connect you with one now"; it now says it can't transfer.
- **No phone numbers.** No verified, location-specific emergency or poison-control number is configured, so neither response names one.
- **No claim of contacting anyone.** "I can't call anyone for you from this line."

If a real handoff (e.g. a Twilio `<Dial>` to a staffed pharmacist line) or a verified local number is added later, change the responses and `test_safety_responses_promise_no_transfer_number_or_action_that_does_not_exist` together.

## Failure behaviour (fail closed)

| Failure | Result |
|---|---|
| Either trigger file missing, empty, without rules, or with an invalid regex | Startup fails (`_load_safety_detector`). Before this change an empty or malformed clinical file silently fell back to the generic *handoff* phrases. |
| Urgent tier raises at runtime | Turn blocked with the clinical response, which also carries the emergency line. This avoids telling every caller it is an emergency. `clinical_guard_errors_total` is incremented. |
| Clinical tier or PolicyEngine raises | Turn blocked (unchanged behaviour, plus the error counter). |
| Metrics or audit backend raises | The safety response is still spoken. Observability is best-effort in the block path, and the pre-guard `requests_total` increment is now best-effort too (it previously could crash a turn before the safety step). |
| Gemini fails, Groq fallback | Irrelevant to a blocked turn: no provider is ever called (`test_gemini_failure_cannot_send_a_blocked_request_to_groq`). |

## LLM defence in depth

`ConversationManager.MEDICAL_SAFETY_PROMPT` is appended to the first system message of every generated turn, including when `system_prompt` is overridden. Gemini and Groq receive the same message list through `FallbackLLMProvider`. It forbids dose, frequency, start/stop/skip/combine advice, interaction or suitability judgments, and symptom interpretation. It tells the model to direct urgent symptoms to the local emergency number, to invent no numbers or transfers, and to ignore requests to drop the rules. `SYSTEM_PROMPT` itself is unchanged because the local fine-tuned model was trained against it.

This is **not** the primary control. It only matters for requests the deterministic tiers miss.

## Observability

Counters (fixed names, no labels, no caller text): `clinical_blocks_urgent_total`, `clinical_blocks_medication_total`, `clinical_guard_errors_total`, plus the existing `policy_denials_total`, `handoffs_total` and `decision_route_safety_total`. The audit `SAFETY_BLOCK` event carries only `confidence`, `rule` (`URGENT_MEDICAL_RISK` / `MEDICAL_DOSAGE` / `SAFETY_CHECK_UNAVAILABLE` / `POLICY_ENGINE_UNAVAILABLE`) and `category`.

## Known limits

- **Keyword rules miss things.** Unusual phrasing, other languages, or speech-to-text errors outside the tested variants can still reach the LLM, where only the prompt rules apply. Review real (consented, de-identified) transcripts and extend the YAML.
- **Over-triggering (safe direction, documented):**
  - Clinical response, new with this change: "can I take a friend with me…", "should I take the bus…".
  - Emergency instruction, new with this change: "can this medicine cause chest pain", "do you sell anything for trouble breathing", "I have a severe rash".
  - Pre-existing in the old guard: "Do I have to sign in again?", "how many items can I take back…", "is it ok for me to pick up later", "what is an overdose".
- **Location:** "local emergency number" and "poison control" are generic on purpose. A deployment in a known country should configure and verify the real numbers.
- **No clinical review.** The categories, wording and responses have not been reviewed by a pharmacist or clinician.
- **Real telephony is unvalidated.** Voice behaviour is proven only in the local simulation with fake providers.
