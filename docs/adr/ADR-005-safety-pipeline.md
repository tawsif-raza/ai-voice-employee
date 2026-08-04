# ADR-005: Shared Matching Engine for the Safety Pipeline, with Fail-Closed Error Handling

**Date:** 2026-08-04
**Architecture Version:** v1.0 (`architecture-v1.0`, commit `a7d567d`)
**Related Documents:**
- `MODULES.md` §5 (Safety Engine), "Important internal note"
- `SEQUENCE_DIAGRAMS.md` — Diagram 5 (Human Handoff Flow)
- `DOMAIN_MODEL.md` — Group 2 (`SafetyResult`)
- `ARCHITECTURE_REVIEW.md` findings 3.2, 5.1

# Status

Accepted

# Context

The platform needs safety decisions that don't depend on trusting the LLM's own judgment, at two distinct points in a turn: before generation (should this message even reach the model — clinical/hard-safety triggers) and after generation (did the model's output itself signal a need to hand off). Both checks need to be deterministic, auditable, and fully offline-testable. The question was whether these are two independently built components or two configurations of one mechanism, and what happens when the mechanism itself fails internally.

# Decision

Safety Engine implements both checks as two configurations of the *same* underlying layered-matching engine (normalize → exact phrase → regex → synonym → semantic-similarity layers), not as two independently implemented components (`MODULES.md` §5's internal note frames this explicitly as intentional reuse). On an internal error (e.g. malformed configuration), the result defaults to `triggered: true` — fail closed — the one deliberate exception to the platform's general fail-safe-and-continue posture; every other module's documented failure behavior degrades and continues, only Safety Engine escalates by default.

# Alternatives Considered

- **Two fully independent implementations** for clinical detection and handoff detection. Not selected: the two checks are structurally the same problem (does this text match a configured pattern set) with different configuration, and building them twice would mean fixing a detection bug in one without the guarantee it's also checked against the other.
- **Fail-open on internal safety error** — treat an internal failure as "not triggered" and let the turn proceed normally. Not selected: `MODULES.md` §5 explicitly reasons that "a safety component running with no rules is worse than not starting," extending the same logic that already governs Safety Engine's config-loading behavior (§12a) to its runtime error handling.
- **Trusting the LLM's own judgment** for handoff detection (e.g. asking the model to self-report when it wants to hand off, with no independent verification). Not selected: this is precisely what Safety Engine's existence is meant to avoid — deterministic decisions "that don't depend on trusting the LLM's judgment" (§5 Purpose).

# Consequences

**Positive**
- One matching engine to maintain, test, and improve — a paraphrase-handling improvement benefits both the clinical and handoff checks simultaneously.
- Fail-closed error handling means a broken safety configuration can never silently degrade into "no safety checks are running."

**Negative**
- A change intended to fix or extend one check (e.g. adding handoff paraphrases) can silently change the other check's behavior (e.g. clinical trigger sensitivity), since they share the same engine.

**Known Limitations**
- Prompt-injection risk from adversarial user input is unaddressed (`ARCHITECTURE_REVIEW.md` 5.1, High severity) — both checks operate on text that could itself be adversarially crafted to evade or manipulate the pattern match.

# Trade-offs

Sharing one engine trades independent-evolution flexibility for consistency and lower maintenance cost — the explicit trade-off documented in `MODULES.md` §5 is that any change to the shared engine "must be regression-tested against both configurations before shipping," turning what could have been a structural guarantee (independent components can't affect each other) into a process discipline (regression tests must be run every time).

# Risks

- `ARCHITECTURE_REVIEW.md` finding 3.2 (High severity) named this exact shared-fate risk as a security-relevant gap not addressed in `ARCHITECTURE.md`'s Security Considerations — the risk is documented here, not resolved.
- Fail-closed is safe by default but could cause over-triggering (false-positive escalations) under a bad configuration deploy — there's no documented distinction between "a rule genuinely matched" and "the engine itself is misconfigured," both surface identically as `triggered: true`.
- Because both checks run through one engine, a regression-test suite that only covers one configuration's test cases could miss a behavior change in the other.

# Future Work

- Build the regression-test discipline `MODULES.md` §5 calls for explicitly — a shared test suite exercised against both the clinical and handoff configurations on every change to the matching engine.
- Address prompt-injection risk from adversarial user input (`ARCHITECTURE_REVIEW.md` 5.1).
- Consider whether a fail-closed result should distinguish "rule matched" from "engine error" in its evidence field, so operators can tell the two apart.
