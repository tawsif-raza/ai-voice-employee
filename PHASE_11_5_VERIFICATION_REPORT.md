# Phase 11.5 — Verification, Environment Repair & Baseline Freeze Report

**Status:** Complete. Verification only — no Phase 12 functionality, no new architecture, no commits.
**Date:** 2026-08-18

## 1. Executive Summary

The previous Phase 11 report claimed ~554 tests passing with one unrelated FAISS environment gap, but that number had never been independently re-run against this working tree — the `.venv` was unusable. This phase repaired the environment, reinstalled the full dependency set, and ran the complete test suite for real.

**Result: 563/563 tests pass. 0 failures, 0 errors, 0 skips.** No genuine regression from Phases 3–11 was found, so no source code was changed in this phase. The previously-reported FAISS gap does not reproduce here — `faiss-cpu==1.15.0` installs and works correctly on this machine's Python 3.14/Windows combination, and all 11 `test_retriever.py` tests pass.

## 2. Step 11.5.1 — Repository State

```
Branch: main (ahead of origin/main by 4 commits)
Last commit: 7923d66 docs: add architecture decision records v1.0
```

Working tree state (unchanged by this phase):
- `docs/API_SPEC.md`, `docs/REPOSITORY_STRUCTURE.md` — staged
- `requirements.txt`, `src/api/server.py`, `src/inference/predict.py`, `docs/REPOSITORY_STRUCTURE.md` — modified, unstaged
- All of `src/agent/`, `src/inference/llm_service.py`, 21 new test files, `configs/policies/`, `configs/auth.yaml`, `configs/intent_taxonomy.yaml`, `configs/reliability.yaml`, `plan.md`, all `PHASE_*_REPORT.md`, `docs/SECURITY.md`, `docs/IMPLEMENTATION_ROADMAP.md`, two new ADRs, `outputs/` — untracked

This confirms all of Phases 3–11's work exists only in the working tree; none of it has been committed. Per this phase's instructions, nothing was committed or staged further.

## 3. Step — Environment Repair

**Root cause found:** `.venv/pyvenv.cfg` recorded `home = C:\Users\Hp\AppData\Local\Python\pythoncore-3.14-64` and `command = ... -m venv D:\AI_VOICE_AGENT\.venv` — this venv was created on a different machine/user account (user `Hp`, drive `D:`) and copied into this repo. Its `Scripts\python.exe` is a small redirect launcher (Python 3.14's "app execution alias" style stub) that reads `pyvenv.cfg` at startup and re-execs the recorded base interpreter path; since that path doesn't exist on this machine, every invocation failed immediately with "did not find executable at ...".

**Fix:** `.venv/` is listed in `.gitignore` (confirmed) — it is disposable local tooling, not tracked work product, so it was safe to discard and recreate without any user work being at risk.

```
rm -rf .venv
"C:\Users\tawsi\AppData\Local\Python\pythoncore-3.14-64\python.exe" -m venv .venv
```

Verified: `.venv/Scripts/python.exe --version` → `Python 3.14.7`.

**Dependency install:** `pip install -r requirements.txt` completed with exit code 0. All packages resolved and installed cleanly, including the heaviest ones (`torch==2.13.0`, `bitsandbytes==0.49.2`, `faiss-cpu==1.15.0`, `sentence-transformers==5.6.1`) — no wheel-availability or build failures on this Python 3.14/Windows combination.

Note: the test suite itself uses the standard library `unittest` runner exclusively (`python -m unittest discover`), not `pytest` — `pytest` is not a project dependency and is not required.

## 4. Step — Full Test Suite Run

```
.venv/Scripts/python.exe -m unittest discover -s tests -v
```

```
----------------------------------------------------------------------
Ran 563 tests in 30.352s

OK
```

- **Failures:** 0
- **Errors:** 0
- **Skipped:** 0 (confirmed by grepping the full verbose log for skip markers — the only "skip" substring hits are test *names* describing skip-related business logic, e.g. `test_no_clinical_guard_configured_skips_the_check`, not actual `unittest` skip outcomes)
- **Test files executed:** all 24 files under `tests/`, including the 3 pre-Phase-3 tracked tests (`test_clinical_guard.py`, `test_handoff_detector.py`, `test_retriever.py`) and all 21 untracked tests added across Phases 3–11

### FAISS gap re-verified as closed

Phase 11's report flagged "one pre-existing, unrelated faiss environment gap." That gap does not exist in this freshly-verified environment: `faiss-cpu==1.15.0` has a native Windows/cp314 wheel, installed without incident, and all `test_retriever.py` tests (index build, persistence, reload, domain filtering, top-k, relevance scoring) pass.

### Benign warnings observed (not defects)

Four warnings appeared in the log, all from third-party libraries, none affecting correctness:
1. `StarletteDeprecationWarning`: `starlette.testclient` prefers `httpx2` over `httpx` — a future-proofing notice from the test client library itself, not this codebase.
2. `Warning: You are sending unauthenticated requests to the HF Hub` — informational; no `HF_TOKEN` is set, which only affects download rate limits.
3–4. `huggingface_hub` cache symlink warning — Windows-specific caching degradation notice from `sentence-transformers`' model download, not a test failure.

## 5. Step — Genuine Regression Search

Because the suite passed at 563/563, there were no failures to triage into "environment" vs. "genuine regression" buckets. As an additional sanity check beyond the test suite itself:

- `import server` (from `src/api/`, with `src/agent/` and `src/inference/` on `sys.path`, matching the project's existing no-`__init__.py` import convention) succeeds silently — the FastAPI app object builds without needing a loaded model or live network calls.
- Reviewed the two files with active uncommitted diffs (`src/inference/predict.py`, `src/api/server.py`) end-to-end. Both changes are internally consistent with the Phase 3–11 reports and `docs/IMPLEMENTATION_ROADMAP.md` milestone M2: `predict.py`'s `VoiceAssistantInference` is now a backward-compatible facade delegating to `ConversationManager`/`LLMService`, and `server.py`'s `/generate` route now calls `ConversationManager.handle_turn()` directly with the Phase 7/9 authentication boundary wired in front of it. No half-finished code paths or dangling TODOs were found in either diff.
- `configs/policies/*.yaml` were spot-checked (e.g. `generation.yaml`) — well-formed, documents its own precedence model in comments, consistent with `policy_engine.py`'s stated conflict-resolution rules.

**No genuine regression was found. No source files were modified in this phase.**

## 6. Compatibility

All of the following, exercised by the 563-test run, are confirmed working together on the freshly-verified environment with no changes required:

- ConversationManager orchestration (clinical → intent → RAG → LLM → handoff)
- PolicyEngine (generation, tool, handoff, privacy, confirmation, authorization)
- ToolOrchestrator + mock tools + LLM trust-boundary attack suite
- SessionManager / MemoryManager, including cross-user isolation and expiry
- PII detection / PrivacyService / privacy-aware logging
- Identity (dev provider) and OIDC (production provider) authentication
- Observability (audit, metrics, correlation IDs) and reliability (retry, circuit breaker)
- Phase 11's full security red-team suite (10 invariants, LLM trust-boundary matrix, concurrency/race tests)
- Pre-existing RAG retriever and clinical/handoff detector suites

## 7. Known Out-of-Scope Gaps (not defects — unchanged from before this phase)

- **No trained model artifact is present** (`outputs/` has no `merged_model` directory) — the test suite mocks the LLM throughout, so this does not affect test verification, but it means `/generate` cannot actually be exercised end-to-end against a real model in this environment without first running training/export. This is expected and out of scope for a verification-only phase.
- **All Phase 3–11 work remains uncommitted.** This phase was explicitly instructed not to commit or tag anything; that instruction was followed. The working tree is otherwise unchanged from before this phase except for `.venv/` (gitignored, not tracked).

## 8. Baseline Statement

As of this verification, on a freshly-repaired, from-scratch `.venv` built against local Python 3.14.7 with `requirements.txt` installed exactly as pinned:

> **563/563 tests pass. 0 failures. 0 errors. 0 skips.** The FAISS environment gap previously reported is closed. No genuine regression exists in the current working tree. This is a clean, independently-verified baseline for Phases 3–11.

## 9. Recommended Next Step

Unchanged from Phase 11's own recommendation, now on a verified footing: real external OIDC provider integration testing, durable (non-in-memory) audit/session/memory storage, and an independent third-party security review. Phase 12 was not started in this phase.
