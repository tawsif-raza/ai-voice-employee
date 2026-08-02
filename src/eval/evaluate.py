"""
Phase 6: Automated Evaluation Pipeline
Goal: Run the voice assistant against a fixed benchmark and report
      response-length, handoff precision/recall, and latency/throughput
      metrics — a repeatable check that a checkpoint didn't regress.
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import yaml

# Reuse VoiceAssistantInference from src/inference/predict.py instead of
# duplicating it. This codebase avoids package-relative imports (no
# __init__.py anywhere), so the sibling directory is added to sys.path
# explicitly rather than importing across src/ subpackages.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "inference"))
from predict import VoiceAssistantInference  # noqa: E402

try:
    from rich.console import Console
    from rich.table import Table
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False


# ── Config ───────────────────────────────────────────────────────────────────
# results_path defaults from configs/config.yaml's `evaluation:` section,
# the same single source of truth src/training/config.py and
# src/export/merge_and_convert.py read. Falls back if the file/key is missing.

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "config.yaml"


def _load_yaml() -> dict:
    if not _CONFIG_PATH.exists():
        return {}
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_YAML = _load_yaml()


def _get(key: str, default: Any) -> Any:
    return _YAML.get("evaluation", {}).get(key, default)


# ── Benchmark ────────────────────────────────────────────────────────────────
# 10 in-domain / 5 handoff-escalation / 5 out-of-domain-edge-case prompts.

BENCHMARK = [
    # In-domain customer-service queries — should answer directly, no handoff.
    {"id": 1, "category": "in_domain", "message": "What are your business hours?", "expected_handoff": False},
    {"id": 2, "category": "in_domain", "message": "How do I reset my account password?", "expected_handoff": False},
    {"id": 3, "category": "in_domain", "message": "What's your return policy for unused items?", "expected_handoff": False},
    {"id": 4, "category": "in_domain", "message": "How long does standard shipping usually take?", "expected_handoff": False},
    {"id": 5, "category": "in_domain", "message": "Do you ship internationally?", "expected_handoff": False},
    {"id": 6, "category": "in_domain", "message": "What payment methods do you accept?", "expected_handoff": False},
    {"id": 7, "category": "in_domain", "message": "How can I track my order?", "expected_handoff": False},
    {"id": 8, "category": "in_domain", "message": "Can I change my shipping address after placing an order?", "expected_handoff": False},
    {"id": 9, "category": "in_domain", "message": "What's your cancellation policy on subscriptions?", "expected_handoff": False},
    {"id": 10, "category": "in_domain", "message": "Do you have a mobile app I can use to manage my account?", "expected_handoff": False},

    # Handoff / escalation queries — should offer to connect to a human.
    {"id": 11, "category": "handoff", "message": "I want to speak to a real human agent right now.", "expected_handoff": True},
    {"id": 12, "category": "handoff", "message": "This is the third time I've contacted you about my refund — I need this escalated.", "expected_handoff": True},
    {"id": 13, "category": "handoff", "message": "I want to file a formal complaint about how I've been treated.", "expected_handoff": True},
    {"id": 14, "category": "handoff", "message": "Can you connect me with a manager? I'm not satisfied with this conversation.", "expected_handoff": True},
    {"id": 15, "category": "handoff", "message": "My card was charged twice for the same order and I need a person to fix it immediately.", "expected_handoff": True},

    # Out-of-domain / edge cases — should decline gracefully, no handoff needed.
    {"id": 16, "category": "out_of_domain", "message": "What's the capital of France?", "expected_handoff": False},
    {"id": 17, "category": "out_of_domain", "message": "Can you write me a short poem about the ocean?", "expected_handoff": False},
    {"id": 18, "category": "out_of_domain", "message": "What's 15 times 37?", "expected_handoff": False},
    {"id": 19, "category": "out_of_domain", "message": "Tell me a joke.", "expected_handoff": False},
    {"id": 20, "category": "out_of_domain", "message": "What's the weather like today?", "expected_handoff": False},
]


# ── Per-case evaluation ──────────────────────────────────────────────────────

def run_case(assistant: VoiceAssistantInference, case: dict) -> dict:
    """
    Run one benchmark case and measure word/token length, handoff
    correctness, time-to-first-token, and generation throughput.
    """
    start = time.perf_counter()
    ttft_ms = None
    final = None

    for item in assistant.generate_response_stream(case["message"]):
        if isinstance(item, str):
            if ttft_ms is None:
                ttft_ms = (time.perf_counter() - start) * 1000
        else:
            final = item

    response_text = final["response"]
    word_count = len(response_text.split())
    token_count = len(assistant.tokenizer.encode(response_text)) if response_text else 0
    latency_ms = final["latency_ms"]
    throughput = token_count / (latency_ms / 1000) if latency_ms > 0 and token_count else 0.0
    predicted_handoff = final["is_handoff"]

    return {
        **case,
        "response": response_text,
        "predicted_handoff": predicted_handoff,
        "correct_handoff": predicted_handoff == case["expected_handoff"],
        "word_count": word_count,
        "token_count": token_count,
        "ttft_ms": ttft_ms if ttft_ms is not None else latency_ms,
        "latency_ms": latency_ms,
        "throughput_tok_s": throughput,
    }


def compute_metrics(results: list[dict]) -> dict:
    word_counts = [r["word_count"] for r in results]
    token_counts = [r["token_count"] for r in results]
    ttfts = [r["ttft_ms"] for r in results]
    throughputs = [r["throughput_tok_s"] for r in results if r["throughput_tok_s"] > 0]

    tp = sum(1 for r in results if r["predicted_handoff"] and r["expected_handoff"])
    fp = sum(1 for r in results if r["predicted_handoff"] and not r["expected_handoff"])
    fn = sum(1 for r in results if not r["predicted_handoff"] and r["expected_handoff"])
    tn = sum(1 for r in results if not r["predicted_handoff"] and not r["expected_handoff"])

    return {
        "num_cases": len(results),
        "accuracy": sum(1 for r in results if r["correct_handoff"]) / len(results) if results else 0.0,
        "avg_word_count": statistics.mean(word_counts) if word_counts else 0.0,
        "avg_token_count": statistics.mean(token_counts) if token_counts else 0.0,
        "avg_ttft_ms": statistics.mean(ttfts) if ttfts else 0.0,
        "avg_throughput_tok_s": statistics.mean(throughputs) if throughputs else 0.0,
        "handoff_precision": tp / (tp + fp) if (tp + fp) else 0.0,
        "handoff_recall": tp / (tp + fn) if (tp + fn) else 0.0,
        "handoff_tp": tp,
        "handoff_fp": fp,
        "handoff_fn": fn,
        "handoff_tn": tn,
    }


# ── Reporting ────────────────────────────────────────────────────────────────

def print_summary_rich(results: list[dict], metrics: dict) -> None:
    console = Console()

    table = Table(title="Evaluation Results", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("Category")
    table.add_column("Handoff (exp/pred)")
    table.add_column("Words", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("TTFT ms", justify="right")
    table.add_column("Latency ms", justify="right")

    for r in results:
        mark = "[green]OK[/green]" if r["correct_handoff"] else "[red]MISS[/red]"
        table.add_row(
            str(r["id"]),
            r["category"],
            f"{r['expected_handoff']}/{r['predicted_handoff']} {mark}",
            str(r["word_count"]),
            str(r["token_count"]),
            f"{r['ttft_ms']:.0f}",
            f"{r['latency_ms']:.0f}",
        )
    console.print(table)

    summary = Table(title="Summary Metrics", show_header=False)
    summary.add_column("Metric")
    summary.add_column("Value", justify="right")
    summary.add_row("Cases", str(metrics["num_cases"]))
    summary.add_row("Handoff accuracy", f"{metrics['accuracy']:.1%}")
    summary.add_row("Handoff precision", f"{metrics['handoff_precision']:.2f}")
    summary.add_row("Handoff recall", f"{metrics['handoff_recall']:.2f}")
    summary.add_row("Avg word count", f"{metrics['avg_word_count']:.1f}")
    summary.add_row("Avg token count", f"{metrics['avg_token_count']:.1f}")
    summary.add_row("Avg TTFT (ms)", f"{metrics['avg_ttft_ms']:.0f}")
    summary.add_row("Avg throughput (tok/s)", f"{metrics['avg_throughput_tok_s']:.2f}")
    console.print(summary)


def print_summary_plain(results: list[dict], metrics: dict) -> None:
    print("\n" + "=" * 78)
    print(f"{'#':>3} {'CATEGORY':<14} {'EXP/PRED':<14} {'OK':<5} {'WORDS':>6} {'TOK':>5} {'TTFT ms':>8} {'LAT ms':>8}")
    print("-" * 78)
    for r in results:
        exp_pred = f"{r['expected_handoff']}/{r['predicted_handoff']}"
        ok = "OK" if r["correct_handoff"] else "MISS"
        print(
            f"{r['id']:>3} {r['category']:<14} {exp_pred:<14} {ok:<5} "
            f"{r['word_count']:>6} {r['token_count']:>5} {r['ttft_ms']:>8.0f} {r['latency_ms']:>8.0f}"
        )
    print("=" * 78)
    print(f"Cases                 : {metrics['num_cases']}")
    print(f"Handoff accuracy      : {metrics['accuracy']:.1%}")
    print(f"Handoff precision     : {metrics['handoff_precision']:.2f}")
    print(f"Handoff recall        : {metrics['handoff_recall']:.2f}")
    print(f"Avg word count        : {metrics['avg_word_count']:.1f}")
    print(f"Avg token count       : {metrics['avg_token_count']:.1f}")
    print(f"Avg TTFT (ms)         : {metrics['avg_ttft_ms']:.0f}")
    print(f"Avg throughput (tok/s): {metrics['avg_throughput_tok_s']:.2f}")
    print("=" * 78 + "\n")


def print_summary(results: list[dict], metrics: dict) -> None:
    if _HAS_RICH:
        print_summary_rich(results, metrics)
    else:
        print_summary_plain(results, metrics)


# ── Entry point ────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate the voice assistant against the fixed benchmark")
    parser.add_argument("--base_model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument(
        "--adapter_path",
        default=None,
        help="LoRA checkpoint directory. Auto-resolved from outputs/checkpoint-final "
             "or the latest outputs/checkpoint-* if omitted.",
    )
    parser.add_argument("--max_new_tokens", type=int, default=150)
    parser.add_argument("--output", default=_get("results_path", "outputs/eval_results.json"))
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    print("=" * 60)
    print("QWEN 2.5 VOICE ASSISTANT — EVALUATION")
    print("=" * 60)
    print(f"Benchmark cases: {len(BENCHMARK)}")

    assistant = VoiceAssistantInference(
        base_model_name=args.base_model,
        adapter_path=args.adapter_path,
        max_new_tokens=args.max_new_tokens,
        temperature=0.0,  # deterministic (greedy) so re-runs are comparable
    )

    results = []
    for i, case in enumerate(BENCHMARK, start=1):
        print(f"[{i}/{len(BENCHMARK)}] {case['category']}: {case['message'][:60]}")
        results.append(run_case(assistant, case))

    metrics = compute_metrics(results)
    print_summary(results, metrics)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({"metrics": metrics, "results": results}, f, indent=2)
    print(f"Saved detailed results to {output_path}")
