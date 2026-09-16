# AUTONOMOUS PROJECT EXECUTION CONTROLLER

## PURPOSE

This file is the authoritative execution roadmap for the project.

The AI engineering agent must treat this file as the primary source of truth for project execution.

The agent must NOT require the user to provide a new prompt for every phase.

The agent must:

1. Read the complete repository before making major decisions.
2. Read this `plan.md`.
3. Determine the current phase from the phase statuses.
4. Read all relevant previous phase reports.
5. Inspect the current code and Git state.
6. Execute the highest-priority eligible phase automatically.
7. Complete that phase according to its acceptance criteria.
8. Run all required validation.
9. Fix defects discovered during implementation.
10. Write the required phase report.
11. Update this `plan.md`.
12. Create the phase commit.
13. Verify the working tree.
14. Automatically continue to the next eligible phase.

The user should NOT have to say:
"Start Phase X."

The repository itself determines what comes next.

---

# EXECUTION RULES

## Rule 1 — Repository First

Before changing code:

- inspect the repository
- inspect `plan.md`
- inspect the relevant `PHASE_*.md` reports
- inspect ADRs
- inspect architecture documentation
- inspect tests
- inspect configuration
- inspect Git state

Never make major changes based solely on this controller.

---

## Rule 2 — Current State Determines Execution

At startup, determine:

- completed phases
- incomplete phases
- blocked phases
- conditional phases
- currently active phase
- dependencies between phases

Use these statuses:

- `NOT_STARTED`
- `IN_PROGRESS`
- `BLOCKED`
- `CONDITIONAL`
- `COMPLETE`
- `VERIFIED`

Never start a phase whose prerequisites are unresolved unless the phase explicitly permits parallel execution.

---

## Rule 3 — Automatic Phase Progression

When a phase satisfies all acceptance criteria:

1. mark it `COMPLETE`
2. create its report
3. commit the changes
4. update `plan.md`
5. identify the next eligible phase
6. begin that phase automatically

Do not stop simply because a phase finished.

Continue until:

- the roadmap is complete
- a genuine blocker exists
- credentials/infrastructure access are required
- an irreversible business decision is required
- a security-critical uncertainty requires user approval

---

## Rule 4 — No Fake Completion

Never convert:

- `NOT RUN` → `PASS`
- `UNVERIFIED` → `PASS`
- simulated → real
- projected → measured
- mocked → externally verified

Evidence must match the claim.

---

## Rule 5 — No Unnecessary User Questions

Do not ask the user about decisions that can be determined from:

1. existing code
2. existing documentation
3. existing ADRs
4. existing tests
5. this plan
6. standard engineering practice
7. engineering judgment

Ask only when a decision genuinely requires:

- credentials
- external infrastructure
- business policy
- legal/compliance direction
- irreversible architecture choice
- missing product requirement

When asking, provide:

Problem
Options
Recommendation
Reason

---

## Rule 6 — Phase Isolation

Each phase must have:

- objective
- scope
- dependencies
- implementation tasks
- validation
- acceptance criteria
- exit criteria
- report

Do not mix unrelated product work into a phase.

---

## Rule 7 — Definition of Done

A phase is complete only when applicable:

- implementation complete
- tests added
- existing tests passing
- lint passing
- formatting passing
- type checking passing
- security reviewed
- integration tests passing
- performance validated where required
- documentation updated
- phase report created
- Git commit created
- working tree clean
- acceptance criteria satisfied

---

## Rule 8 — Preserve Existing Architecture

Do not rewrite working systems unnecessarily.

Preserve:

- ConversationManager
- ClinicalGuard
- PolicyEngine
- Handoff detection
- ToolOrchestrator
- session architecture
- persistence
- privacy/PII controls
- authentication
- audit system
- observability
- provider abstraction
- voice pipeline

Refactor only when evidence justifies it.

---

## Rule 9 — Production Evidence

When a phase concerns production readiness, distinguish:

LOCAL
SIMULATED
MOCKED
INTEGRATION
LIVE
CANARY
PRODUCTION

Never mix these categories.

---

## Rule 10 — Security

Security takes priority over feature velocity.

Never:

- expose secrets
- commit credentials
- weaken authorization
- bypass safety controls
- trust LLM output as authorization
- allow cross-session state leakage
- disable security tests to make CI pass

---

## Rule 11 — External Integrations

External providers must be validated honestly.

Provider examples:

- Anthropic Claude
- Google Gemini
- Groq
- Twilio
- Deepgram
- ElevenLabs
- PostgreSQL
- OpenTelemetry

Live credentials must only be used through environment variables or secure secret stores.

---

## Rule 12 — Provider Strategy

The AI application must remain provider-agnostic.

Current intended hierarchy:

Primary:
Claude

Fallback:
Gemini

Optional emergency/free fallback:
Groq

Optional local fallback:
Qwen/local model

Do not hardcode business logic to a specific provider.

The actual provider hierarchy must remain configuration-driven.

---

## Rule 13 — Testing

Never reduce the existing test suite to make a phase pass.

Every regression must be investigated.

New behavior requires appropriate new tests.

External dependencies should be mocked in normal CI.

Live tests must remain opt-in.

---

## Rule 14 — Phase Reports

Every completed phase must produce:

`docs/PHASE_<N>_<NAME>_REPORT.md`

The report must include:

- objective
- implementation
- files changed
- tests
- metrics
- security impact
- known limitations
- acceptance criteria
- final status
- next phase

---

## Rule 15 — Git

At the completion of every phase:

- review Git diff
- ensure no unrelated changes
- commit the phase
- verify commit
- verify clean working tree

---

## Rule 16 — Automatic Recovery

If a phase discovers a defect:

1. reproduce it
2. determine root cause
3. fix it
4. add regression test
5. rerun relevant tests
6. rerun complete validation
7. continue

Do not stop merely because the first implementation attempt failed.

---

## Rule 17 — Phase Dependencies

A blocked phase must not automatically block unrelated phases unless a dependency exists.

For example:

- external Twilio validation may block public telephony release
- but documentation, observability, CI improvements, and offline architecture work may continue if independent

Use dependency-aware execution.

---

## Rule 18 — Final Project Goal

The final goal is:

A secure, reliable, observable, scalable production AI voice-agent platform capable of real-time calls, deterministic safety enforcement, authenticated patient workflows, provider failover, tool execution, persistence, monitoring, recovery, and controlled production deployment.

Do not optimize for "number of completed phases."

Optimize for a working production system.





# PHASE 17 — EXTERNAL INTEGRATION CLOSURE

Status: BLOCKED until real external environment exists.

Re-confirmed 2026-09-16 (Phase 18 startup check): no `.env` with real
credentials exists in this repository (only `.env.example`/
`.env.canary.example` templates), no reachable public HTTPS/WSS staging
deployment exists, and per `docs/phase1.4-external-integration-report.md`
the user has explicitly declined to supply `ANTHROPIC_API_KEY` (a
standing business decision) and no Twilio account/staging endpoint has
been provisioned. Gemini alone has real LIVE-PASS evidence (Section 4a
of that report); Twilio has zero real evidence. Remains BLOCKED. Per
Rule 17, this does not block Phase 18 (no dependency on live external
credentials).

Objective:
Complete the two remaining real-condition validation gates.

Dependencies:
- Phase 16 complete
- LIVE_VERIFICATION_RUNBOOK.md available
- staging environment available

Tasks:

1. Run live Claude verification.
2. Run live Gemini verification.
3. Validate Claude → Gemini failover.
4. Validate optional Gemini → Groq fallback if configured.
5. Validate real Deepgram integration.
6. Validate real ElevenLabs integration.
7. Deploy/reach the Twilio canary endpoint.
8. Validate Twilio webhook.
9. Validate Twilio Media Streams WebSocket.
10. Execute controlled inbound calls.
11. Validate barge-in.
12. Validate safety/handoff.
13. Validate authentication.
14. Validate session isolation.
15. Validate disconnect/recovery.
16. Record real latency.
17. Produce final external verification evidence.

Acceptance:

- No false PASS.
- No unresolved P0 issue.
- Real provider validation recorded.
- Real Twilio validation recorded.
- Real rollback tested.

Deliverable:

docs/PHASE_17_EXTERNAL_INTEGRATION_CLOSURE_REPORT.md

Exit status:

VERIFIED
or
CONDITIONAL
or
BLOCKED



# PHASE 18 — PRODUCTION SECURITY GATE

Status: COMPLETE (2026-09-16). See PHASE_18_PRODUCTION_SECURITY_GATE_REPORT.md.
One HIGH finding (F-04: telephony caller-PIN universal bypass +
compounding non-functional-permissions defect) found and fixed, with
regression tests. No unresolved P0/P1. Full suite: 912 passed, 0 failed.
Next eligible: Phase 20 (Phase 19 requires the same live Twilio/PSTN
access Phase 17 is blocked on).

Objective:

Perform a final security review before public exposure.

Tasks:

1. Threat model.
2. Authentication review.
3. Authorization review.
4. PIN/OTP security.
5. Brute-force prevention.
6. Session isolation.
7. Tool authorization.
8. Prompt-injection testing.
9. PII protection.
10. Secret management.
11. Twilio signature verification.
12. WebSocket trust boundary.
13. API authentication.
14. Database authorization.
15. Audit logging.
16. Security regression testing.

Perform adversarial tests against:

- authentication
- authorization
- tool execution
- identity switching
- session takeover
- replay
- prompt injection
- malicious voice input
- malformed provider responses

Acceptance:

No unresolved P0/P1 security vulnerabilities.

Deliverable:

docs/PHASE_18_SECURITY_GATE_REPORT.md


# PHASE 19 — REAL-WORLD VOICE QUALITY

Objective:

Improve real telephone conversational quality.

Tasks:

1. Real PSTN call testing.
2. Speakerphone testing.
3. Background noise testing.
4. Accent variation testing.
5. Silence testing.
6. Interruptions.
7. Rapid speech.
8. Slow speech.
9. Repeated speech.
10. Ambiguous utterances.
11. Number/date/name recognition.
12. STT endpointing tuning.
13. TTS pacing.
14. Response length tuning.
15. Barge-in refinement.

Measure:

- STT accuracy
- interruption latency
- response latency
- completion latency
- conversation recovery
- failed turns

Acceptance:

Real callers can complete representative workflows without major conversational breakdowns.


# PHASE 20 — VOICE WORKFLOW COMPLETENESS

Status: COMPLETE (2026-09-16). See PHASE_20_VOICE_WORKFLOW_REPORT.md.
All 4 registered tools (BOOK_APPOINTMENT, CANCEL_APPOINTMENT,
RESCHEDULE_APPOINTMENT, ORDER_LOOKUP) were already voice-reachable via
the shared ConversationManager.handle_turn() path; no missing voice
adapter existed. Found and fixed 3 further defects in the shared
telephony-authentication plumbing all 4 depend on: F-05 (HIGH, voice
pipeline read caller-authenticated state from the wrong object, making
the whole PIN feature non-functional end-to-end regardless of Phase
18's F-04 fix), F-06 (MEDIUM, same non-functional-permissions bug as
F-04, in voice_pipeline.py's own AuthContext construction), F-07 (LOW,
AWAITING_AUTHENTICATION not cleared on barge-in). 2 new regression
tests. No unresolved P0/P1. Full suite: 914 passed, 0 failed. Next
eligible: Phase 21 (Phase 19 requires the same live PSTN/Twilio access
Phase 17 is blocked on).

Objective:

Ensure every critical business workflow can be completed through the voice channel.

Evaluate existing ToolOrchestrator tools.

For each tool determine:

- supported by voice?
- authentication required?
- confirmation required?
- safety review required?
- error recovery?
- retry behavior?
- cancellation?
- interruption?
- audit event?

Implement missing voice adapters.

Acceptance:

Every approved voice workflow has a complete:

Caller → STT → policy → tool → result → TTS

path.

Deliverable:

docs/PHASE_20_VOICE_WORKFLOW_REPORT.md


# PHASE 21 — PERFORMANCE & LOAD

Status: COMPLETE (2026-09-16). See PHASE_21_PERFORMANCE_REPORT.md.
New scripts/performance_load_test.py (Phase 16 harness pattern reused).
Default construction path (max_concurrent_generations=1) confirmed fully
serial (re-confirms a known, disclosed Phase 16 characteristic); factory/
remote-provider-configured path (max_concurrent_generations=20) handled
5/20/50 concurrent jobs with 0 errors; 300 sustained sequential requests:
0 errors, 0MB memory growth; 20 concurrent requests against real local
Postgres: 0 errors, p99 50ms (LIVE). Scaling/failure threshold NOT
FOUND (load never pushed high enough to break -- disclosed honestly, not
guessed). WebSocket/Twilio load NOT RUN (needs live Twilio, blocked same
as Phase 17). No unresolved P0/P1; no application code changed (pure
measurement pass). Next eligible: Phase 22.

Objective:

Determine the actual system capacity.

Tasks:

1. Concurrent calls.
2. Sustained calls.
3. Long-duration calls.
4. Provider throttling.
5. DB connection pressure.
6. WebSocket pressure.
7. CPU.
8. Memory.
9. background task accumulation.
10. queue growth.
11. latency degradation.

Measure:

p50
p95
p99
max

Determine:

- safe concurrency
- bottleneck
- scaling threshold
- failure threshold
- recovery time

Acceptance:

Documented performance envelope.

Deliverable:

docs/PHASE_21_PERFORMANCE_REPORT.md



# PHASE 22 — OBSERVABILITY & INCIDENT RESPONSE

Objective:

Make the system operable without the developer watching the terminal.

Tasks:

1. OpenTelemetry traces.
2. Metrics.
3. structured logs.
4. call correlation IDs.
5. provider correlation.
6. failure metrics.
7. latency metrics.
8. failover metrics.
9. authentication metrics.
10. safety/handoff metrics.
11. alert definitions.
12. dashboards where appropriate.
13. incident runbook.
14. rollback procedure.

Ensure tracing failures cannot affect requests.

Acceptance:

An engineer can diagnose a failed call from observability data without reproducing it locally.


# PHASE 23 — COST AND PROVIDER OPTIMIZATION

Objective:

Optimize provider usage without sacrificing safety or quality.

Evaluate:

Claude
Gemini
Groq
local model
STT
TTS
Twilio

Measure:

cost per minute
cost per call
fallback rate
provider utilization
average response tokens

Implement only justified optimizations.

Possible strategies:

- model selection
- response limits
- caching
- prompt optimization
- routing
- cheaper fallback
- local processing where appropriate

Do not optimize purely for lowest cost.

Optimize:

Reliability + Quality + Latency + Cost.


# PHASE 24 — DATA AND PRIVACY HARDENING

Objective:

Review the complete lifecycle of user data.

Audit:

- transcripts
- audio
- conversations
- audit events
- logs
- database
- caches
- temporary files
- traces
- provider requests
- provider responses

Determine:

What is stored?
Why?
For how long?
Who can access it?

Implement:

- retention policies
- deletion
- redaction
- safe logging
- provider data minimization

Never store data merely because it is technically available.


# PHASE 25 — DISASTER RECOVERY

Objective:

Ensure the system can recover from infrastructure failure.

Test:

- database outage
- application restart
- container restart
- provider outage
- Twilio disconnect
- Redis/cache failure if applicable
- network interruption
- corrupted temporary data
- partial deployment

Verify:

- recovery
- data consistency
- audit consistency
- session cleanup
- rollback

Document RTO/RPO assumptions where applicable.

Deliver:

docs/PHASE_25_DISASTER_RECOVERY_REPORT.md


# PHASE 26 — PRODUCTION DEPLOYMENT

Objective:

Deploy the verified system to production safely.

Tasks:

1. Production infrastructure.
2. Secrets.
3. DNS.
4. HTTPS.
5. database.
6. migrations.
7. monitoring.
8. Twilio configuration.
9. provider credentials.
10. health checks.
11. readiness.
12. rollback.
13. deployment validation.
14. smoke tests.

Use canary deployment before full traffic.

Acceptance:

Production deployment succeeds and rollback is verified.


# PHASE 27 — PRODUCTION CANARY

Objective:

Validate the production deployment with controlled traffic.

Start with:

- internal users
- limited calls
- controlled workflows

Monitor:

- errors
- latency
- provider failovers
- safety events
- authentication
- tool failures
- resource usage

Expand traffic gradually.

Acceptance:

No critical production incidents during canary window.

Deliver:

docs/PHASE_27_PRODUCTION_CANARY_REPORT.md


# PHASE 28 — GENERAL AVAILABILITY

Objective:

Determine whether the system is ready for general use.

Verify:

- security
- reliability
- performance
- cost
- observability
- recovery
- authentication
- safety
- provider failover
- operational readiness
- documentation
- support procedures

Release criteria:

No unresolved P0/P1 issues.

All critical workflows verified.

Rollback available.

Monitoring active.

Incident response documented.

Final decision:

GO
or
CONDITIONAL GO
or
NO-GO

Deliver:

docs/PHASE_28_GENERAL_AVAILABILITY_REPORT.md


# AUTONOMOUS EXECUTOR — PERMANENT INSTRUCTION

Whenever the AI coding agent starts work in this repository:

1. Read this entire `plan.md`.
2. Read the current Git status.
3. Read the most recent phase report.
4. Determine the highest-priority incomplete phase.
5. Check its prerequisites.
6. If prerequisites are satisfied, execute the phase automatically.
7. Do not ask the user to manually restate the phase.
8. Do not wait for a second prompt.
9. Execute implementation.
10. Execute tests.
11. Execute static checks.
12. Execute integration tests where possible.
13. Fix discovered defects.
14. Repeat validation until stable.
15. Write the phase report.
16. Update phase status in `plan.md`.
17. Commit the completed phase.
18. Verify the working tree is clean.
19. Re-read `plan.md`.
20. Automatically move to the next eligible phase.

STOP ONLY WHEN:

- a phase is genuinely blocked,
- required external credentials are unavailable,
- required infrastructure does not exist,
- a destructive/irreversible action requires approval,
- a business requirement cannot be inferred,
- a security decision requires human approval.

When blocked:

DO NOT simply stop.

Instead report:

BLOCKER
WHY IT BLOCKS
WHAT HAS ALREADY BEEN COMPLETED
EXACT INPUT REQUIRED
EXACT COMMAND/ACTION NEEDED TO UNBLOCK
WHAT WILL HAPPEN AFTER UNBLOCKING

Then remain idle until the blocker is resolved.

When unblocked, automatically resume from the current phase.

Never restart completed work unnecessarily.

Never mark an incomplete phase complete.

Never fabricate validation.

The repository state is the source of truth.

