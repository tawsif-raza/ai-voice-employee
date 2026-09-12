# Phase 14.1 - Development Workflow Hardening Report

## 1. Problem Discovered

Going into this phase, the repository had **zero** static-analysis tooling of any kind: no linter, no formatter, no type checker, and no `.gitattributes` (line endings were already causing "LF will be replaced by CRLF" noise on nearly every `git add`/`commit` in this session's own transcript). CI (added in the immediately-preceding `ci:` commit) ran only the test suite. This phase's own investigation additionally found 19 source/doc/script files carrying a UTF-8 byte-order mark (BOM) — all from the ad hoc voice/telephony/canary track — which is silently tolerated by CPython but crashes `ruff format` outright, a genuine, previously-invisible reproducibility hazard.

## 2. Tools Selected

| Concern | Tool | Why |
|---|---|---|
| Linting | **ruff** (`ruff check`) | Single Rust binary replacing flake8 + pyflakes + isort + bugbear + pycodestyle; the de facto modern standard; already fast enough to run on every push without a caching strategy. |
| Formatting | **ruff format** | Same binary as linting -- avoids a second tool (e.g. Black) with its own config surface and version-compatibility matrix to maintain. Black-compatible output. |
| Type checking | **mypy** + SQLAlchemy's own `sqlalchemy.ext.mypy.plugin` | The long-established standard; the SQLAlchemy plugin is maintained by SQLAlchemy itself (not third-party), ships inside the `sqlalchemy` package already in `requirements.txt`, and is a config-only addition (no source changes) that resolved ~30 of the 125 baseline errors caused by classic declarative `Column(...)` typing friction. |
| Line endings | **`.gitattributes`** (`text=auto eol=lf`, `*.ps1 eol=crlf`) | Git-native, zero additional tooling. |

**Deliberately not adopted this phase**: pyupgrade (`UP`) and flake8-simplify/comprehensions (`SIM`/`C4`) ruff rule categories (§10), and a pre-commit-framework (§9).

## 3. Configuration Changes

All in `pyproject.toml` (new file):

- **`[tool.ruff]`**: `line-length = 120`, `target-version = "py312"`.
- **`[tool.ruff.lint]`**: `select = ["F", "W", "I", "B", "E4", "E7", "E9"]`, with `E501` and `E402` explicitly disabled (not grandfathered — turned off outright) for two documented, structural reasons:
  - `E501` (line-too-long): this codebase's dense inline-documentation style (see any module docstring in `src/agent/`) intentionally produces long comment/docstring lines; code-line length is already governed by the formatter's own `line-length`, so this rule would only ever flag comments/docstrings — pure churn, zero functional value.
  - `E402` (import-not-at-top): this codebase's own documented convention (see `src/api/server.py`'s module docstring: *"This codebase avoids package-relative imports... so the sibling directory is added to sys.path explicitly"*) requires `sys.path.insert(...)` immediately before an import in nearly every `src/`/`tests/` file. That's a deliberate architectural pattern, not a defect.
- **`[tool.ruff.lint.per-file-ignores]`**: 18 files carry a **grandfathered, non-growing** list of pre-existing `flake8-bugbear` findings (27 occurrences total: `B017` assert-blind-exception, `B904` raise-without-from, `B007` unused-loop-var, `B905` zip-without-strict, `B023` loop-variable-closure, `B008` call-in-default-arg) — real code-quality items, but fixing them means touching exception-handling/loop logic across persistence, OIDC, and voice-pipeline code, which is out of scope for a phase that must not rewrite business logic. New files may not be added to this list.
- **`[tool.mypy]`**: `python_version = "3.12"`, `ignore_missing_imports = true` (many third-party ML libraries — transformers/trl/peft — ship poor or no type stubs; this is the standard, non-strict starting point, not the strictest possible config), the SQLAlchemy plugin, and **25 `[[tool.mypy.overrides]]` blocks**, one per module with a pre-existing error, each disabling *exactly* the error code(s) that module has today (e.g. `conversation_manager` disables `arg-type`/`union-attr` only — a *new* `assignment` error there would still fail CI). Generated from a real `mypy src/` run, not hand-guessed.
- **`.gitattributes`** (new file): normalizes text files to `eol=lf` (Python, YAML, JSON, TOML, Markdown, shell), explicitly keeps `*.ps1` at `eol=crlf` (Windows-only scripts), and marks binary extensions (`*.pt`, `*.safetensors`, `*.db`, images, archives) as `binary` so line-ending normalization never touches them.

## 4. CI Changes

`.github/workflows/ci.yml` gained three steps, ordered fastest-and-most-mechanical first so a trivial slip fails before the (slower) test suite runs:

```
install deps -> ruff format --check -> ruff check -> mypy src/ -> pytest tests/
```

## 5. Dependency Changes

`requirements-dev.txt` gained `ruff==0.16.4` and `mypy==2.3.1`, pinned to the exact versions used to establish this phase's baseline (installing a newer ruff/mypy later could introduce new rules/checks the baseline wasn't built against). No `sqlalchemy[mypy]` extra needed — the plugin ships in the base `sqlalchemy` package already declared in `requirements.txt`.

## 6. Reproducibility Findings

- **Confirmed clean**: a fresh checkout following the README's own setup steps, then `pip install -r requirements.txt && pip install -r requirements-dev.txt`, produces an environment in which all four CI commands (`ruff format --check`, `ruff check`, `mypy src/`, `pytest tests/`) run exactly as they do in this session — verified by running each with `python -m <tool>` exactly as CI invokes them (Windows note added to the README: pip may install `ruff.exe`/`mypy.exe` outside `PATH`, so both the README and CI use the `python -m` form to be platform-independent).
- **Genuine bug found and fixed**: 19 files (`.py`, `.md`, `.yml`, `.ps1`, `.sh`, `.env.canary.example`) carried a UTF-8 BOM, invisible in normal editors/CPython but fatal to `ruff format` (a real crash, not a lint complaint). Stripped from all 19; verified with the full test suite before and after.
- **Not a new finding, re-confirmed**: `pytest`/`pytest-asyncio` were the prior session's undeclared-dependency discovery (fixed in the immediately-preceding `ci:` commit) — this phase's dependency-installation review re-verified no further undeclared runtime dependencies exist beyond that fix.
- **Disclosed, not fixed**: the local development environment for this session runs Python 3.14, while the README documents "Python 3.12+" and CI pins `3.12` explicitly. Both 3.12 (CI) and 3.14 (this session, throughout) run the full suite cleanly, so this is not currently a functional problem, but it means the *documented minimum* has never actually been exercised in this environment. Recommend adding a Python 3.12 job to CI's matrix in a future phase if that gap matters, rather than treating this report's single-version verification as covering it.

## 7. Documentation

`README.md` gained a "Code Quality (lint, format, type-check)" section (after "Running Tests") with the exact local commands matching CI, the Windows `PATH` caveat, a pointer to `pyproject.toml`'s grandfathered-list comments, and an explicit statement that there is no pre-commit hook — CI is the enforcement point (see §11 for why).

## 8. Test Baseline

**Before this phase**: 812 passed, 0 failed (Phase 14's own final count).
**After this phase**: **812 passed, 0 failed** — identical count. No tests were added, removed, or disabled; every change in this phase (256 mechanical/manual lint fixes, a 96-file `ruff format` pass, 19 BOM strips) was verified against the full, unchanged test suite immediately after being applied, not just once at the end.

## 9. Known Limitations

- The 27 grandfathered bugbear findings (§3) and the ~95-error-worth of mypy baseline overrides (§3) are real, disclosed technical debt, not resolved by this phase — see §10 for what fixing them would require.
- `ruff format` was applied to the entire existing codebase in one pass (96 of 111 files). This was a deliberate, considered choice, not an oversight: Section 2 of this phase's own instructions requires CI to enforce formatting, and `ruff format --check` has no baseline/exemption mechanism (unlike `ruff check`'s per-file-ignores or mypy's per-module overrides) — there is no way to add the formatter to CI without the codebase already being formatted. The pass was purely mechanical (verified behaviorally identical via the full test suite before and after) but it is a large diff; reviewers should expect whitespace/line-wrapping changes throughout, not logic changes.
- `tests/` is intentionally not type-checked (CI runs `mypy src/` only) — typing test doubles/fixtures (`Fake*`, `Mock*`, per-test dicts) precisely would add ongoing annotation overhead the tests' own runtime assertions already cover.
- No pre-commit-framework hook was added. Given "prefer a modern, fast, low-maintenance toolchain" and "do not introduce unnecessary tooling" (this phase's own instructions), and that this repository's actual workflow to date has been direct commits to `main` rather than a PR-per-change flow, a pre-commit hook would be one more piece of per-developer-machine configuration to keep working, for a benefit CI already provides at the point that actually matters (before merge). Documented as a deliberate choice in the README, not a gap.

## 10. Remaining Technical Debt (genuine only)

- **SQLAlchemy `Column`-vs-instance typing** (~30 of the mypy baseline's errors, across `audit_repository_postgres.py`, `memory_repository_postgres.py`, `session_repository_postgres.py`): nullable DB columns typed `Optional` at the ORM level being passed into dataclasses that declare non-Optional `str`/`datetime` fields. Likely a real (if narrow) design tension — these fields may be enforced non-null by a `NOT NULL` DB constraint that isn't mirrored in the Python type — worth a dedicated look, not a "workflow hardening" fix.
- **ML-library typing friction** (`llm_service.py`, `merge_and_convert.py`, `dataset.py`, `evaluate.py`): `transformers`/`trl`/`peft` have limited/incorrect type stubs upstream; mypy's complaints here are largely about those libraries' own typing, not this codebase's logic.
- **Async base-class/override signature mismatches** (`stt_service.py`, `tts_service.py`, `voice_pipeline.py`): mypy flags `receive_events`/`synthesize_stream` overrides as returning a coroutine-of-an-async-iterator rather than being declared as async generators directly. This may be a genuine, if cosmetic, signature inconsistency worth a real look in a future phase — not touched here since it's voice-pipeline business logic.
- The 27 grandfathered bugbear findings (§3) are all individually small, real fixes (mostly: name the caught exception and `raise ... from err`, or add `strict=True` to a `zip()`) — good first-issue-sized cleanup for a future pass, deliberately not bundled into this one.
- Neither `ruff`'s nor mypy's rule sets were maximized — see §2's "deliberately not adopted" list. Revisit once the codebase has had a chance to absorb this baseline without churn fatigue.

## 11. Recommendation for Phase 15

The workflow is now enforceable and reproducible: every push/PR to `main` is gated on formatting, linting, type-checking, and the full test suite, all runnable identically on a clean checkout with no undocumented local-machine state. **No critical workflow issue remains blocking Phase 15.** Proceed to Phase 15 as originally planned (production sampling/cost tuning for tracing, per `PHASE_14_TELEMETRY_TRACING_REPORT.md` §12), with this phase's grandfathered-debt lists available as a ready-made backlog for incidental cleanup whenever those specific files are next touched for product reasons.

`PHASE 14.1 COMPLETE`
