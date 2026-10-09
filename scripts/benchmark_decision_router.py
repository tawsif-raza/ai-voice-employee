"""
Decision/Routing Layer before/after benchmark (scripts/benchmark_decision_router.py).

Runs representative turns through the real ConversationManager twice --
without a DecisionRouter (the previous, unconditional path) and with the
shipped one (configs/decision_routing.yaml) -- and reports, per turn:
route, decision latency, total turn latency, whether the LLM was called,
and the prompt size it was sent.

What is measured vs modelled:
- Decision latency and everything ConversationManager does locally
  (clinical guard, intent, policy, router, prompt build) are MEASURED.
- The LLM itself is a local stand-in that sleeps --llm-latency-ms per call
  (default 1159.7 ms: the one live Gemini request recorded in
  docs/phase1.4-live-provider-results.json -- a single sample, not a p50).
  No network, no API key, no spend.
- Retrieval is off, matching docker/Dockerfile.production (RAG disabled).
- Tokens are estimated as prompt characters / 4. Cost is only printed when
  --usd-per-1k-input-tokens is given; this script never assumes a price.

Usage:
    python scripts/benchmark_decision_router.py [--runs 20] [--llm-latency-ms 1159.7] [--json out.json]
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("src/agent", "src/inference"):
    sys.path.insert(0, str(_ROOT / _sub))

from conversation_manager import build_conversation_manager  # noqa: E402
from decision_router import DecisionRouter  # noqa: E402
from intent_engine import IntentEngine  # noqa: E402
from llm_provider import BaseLLMProvider  # noqa: E402

LIVE_GEMINI_SAMPLE_MS = 1159.7

SCENARIOS = [
    ("simple", "Hello"),
    ("faq", "What are your opening hours?"),
    ("appointment", "When is my appointment?"),
    ("clinical", "Can I take this medicine twice a day?"),
    (
        "complex",
        "I ordered a few things last week for my mother and some arrived damaged while others never came, "
        "so what is the best way to sort all of that out before her birthday on Friday?",
    ),
]


class SimulatedLLM(BaseLLMProvider):
    provider_name = "gemini"

    def __init__(self, latency_ms: float):
        self.latency_s = latency_ms / 1000.0
        self.calls = 0
        self.prompt_chars = 0

    def generate_stream(self, messages, **kwargs):
        self.calls += 1
        self.prompt_chars += sum(len(str(m.get("content", ""))) for m in messages)
        time.sleep(self.latency_s)
        text = "Thanks for your question -- here is what I can tell you."
        yield text
        yield {"text": text, "latency_ms": self.latency_s * 1000.0, "provider": self.provider_name}


def _run_turn(manager, llm, text):
    calls_before, chars_before = llm.calls, llm.prompt_chars
    started = time.perf_counter()
    final = list(manager.handle_turn(text))[-1]
    total_ms = (time.perf_counter() - started) * 1000.0
    return {
        "total_ms": total_ms,
        "llm_calls": llm.calls - calls_before,
        "prompt_chars": llm.prompt_chars - chars_before,
        "decision": final.get("decision"),
        "handoff": final.get("is_handoff"),
        "clinical_block": final.get("clinical_guard_triggered"),
    }


def _manager(llm, routing_enabled):
    return build_conversation_manager(
        llm_provider=llm, rag_enabled=False, persistence_enabled=False, decision_routing_enabled=routing_enabled
    )


def _decision_latency(runs):
    """decide() alone, isolated from the rest of the turn."""
    router, engine = DecisionRouter.from_config_file(), IntentEngine()
    samples = []
    for _ in range(runs):
        for _, text in SCENARIOS:
            routing = engine.classify(text)
            started = time.perf_counter()
            router.decide(
                text, routing, generation_action="ALLOW", clinical_confidence=0.0, tool_action=None,
                retriever_available=False,
            )  # fmt: skip
            samples.append((time.perf_counter() - started) * 1000.0)
    samples.sort()
    return {
        "samples": len(samples),
        "p50_ms": statistics.median(samples),
        "p95_ms": samples[int(len(samples) * 0.95) - 1],
        "max_ms": samples[-1],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--llm-latency-ms", type=float, default=LIVE_GEMINI_SAMPLE_MS)
    parser.add_argument("--usd-per-1k-input-tokens", type=float, default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    rows = []
    for name, text in SCENARIOS:
        row = {"scenario": name}
        for label, enabled in (("before", False), ("after", True)):
            llm = SimulatedLLM(args.llm_latency_ms)
            manager = _manager(llm, enabled)
            results = [_run_turn(manager, llm, text) for _ in range(args.runs)]
            row[label] = {
                "total_p50_ms": statistics.median(r["total_ms"] for r in results),
                "llm_calls_per_turn": results[0]["llm_calls"],
                "prompt_tokens_est": results[0]["prompt_chars"] // 4,
                "route": (results[0]["decision"] or {}).get("route", "-"),
                "decision_ms": (results[0]["decision"] or {}).get("latency_ms"),
                "clinical_block": results[0]["clinical_block"],
                "handoff": results[0]["handoff"],
            }
        rows.append(row)

    decision = _decision_latency(max(args.runs, 200))

    print(f"LLM stand-in latency: {args.llm_latency_ms:.1f} ms/call (modelled; RAG off as in the production image)")
    print(f"Runs per scenario: {args.runs}\n")
    header = f"{'scenario':<12} {'route':<14} {'decide ms':>9} {'before p50':>11} {'after p50':>10} {'LLM b/a':>8} {'tok b/a':>10}"
    print(header)
    print("-" * len(header))
    for row in rows:
        b, a = row["before"], row["after"]
        print(
            f"{row['scenario']:<12} {a['route']:<14} {a['decision_ms'] or 0:>9.3f} "
            f"{b['total_p50_ms']:>9.1f}ms {a['total_p50_ms']:>8.1f}ms "
            f"{b['llm_calls_per_turn']:>3}/{a['llm_calls_per_turn']:<4} "
            f"{b['prompt_tokens_est']:>4}/{a['prompt_tokens_est']:<5}"
        )
    print(
        f"\ndecide() alone over {decision['samples']} calls: p50 {decision['p50_ms']:.4f} ms, "
        f"p95 {decision['p95_ms']:.4f} ms, max {decision['max_ms']:.4f} ms (budget 50 ms)"
    )

    calls_before = sum(r["before"]["llm_calls_per_turn"] for r in rows)
    calls_after = sum(r["after"]["llm_calls_per_turn"] for r in rows)
    tokens_saved = sum(r["before"]["prompt_tokens_est"] - r["after"]["prompt_tokens_est"] for r in rows)
    print(f"LLM calls for this mix: {calls_before} before, {calls_after} after; ~{tokens_saved} prompt tokens avoided")
    if args.usd_per_1k_input_tokens is not None:
        print(f"Input cost avoided for this mix: ${tokens_saved / 1000 * args.usd_per_1k_input_tokens:.6f}")

    if args.json:
        args.json.write_text(
            json.dumps({"llm_latency_ms": args.llm_latency_ms, "rows": rows, "decide": decision}, indent=2), "utf-8"
        )


if __name__ == "__main__":
    main()
