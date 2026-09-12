
"""
Phase 2: Dataset Analysis
Goal: Understand structure, quality, and issues before cleaning
"""

from datasets import load_dataset
import statistics
import json


def analyze_dataset(name: str, split: str = "train") -> None:
    """
    Perform a full structural and quality analysis of a dataset.

    Args:
        name: HuggingFace dataset name
        split: Which split to load
    """
    print(f"\n{'='*60}")
    print(f"ANALYSIS: {name}")
    print(f"{'='*60}")

    ds = load_dataset(name, split=split)

    # ── 1. Basic info ──────────────────────────────────────────
    print(f"\n[1] BASIC INFO")
    print(f"  Total examples : {len(ds)}")
    print(f"  Columns        : {ds.column_names}")

    # ── 2. Category distribution ───────────────────────────────
    print(f"\n[2] CATEGORY DISTRIBUTION")
    categories = ds["category"]
    category_counts: dict[str, int] = {}
    for c in categories:
        category_counts[c] = category_counts.get(c, 0) + 1
    for cat, count in sorted(category_counts.items(), key=lambda x: -x[1]):
        bar = "█" * (count // 200)
        print(f"  {cat:<30} {count:>5}  {bar}")

    # ── 3. Intent distribution (top 10) ────────────────────────
    print(f"\n[3] TOP 10 INTENTS")
    intents = ds["intent"]
    intent_counts: dict[str, int] = {}
    for i in intents:
        intent_counts[i] = intent_counts.get(i, 0) + 1
    top_intents = sorted(intent_counts.items(), key=lambda x: -x[1])[:10]
    for intent, count in top_intents:
        print(f"  {intent:<40} {count:>5}")

    # ── 4. Response length analysis ────────────────────────────
    print(f"\n[4] RESPONSE LENGTH ANALYSIS (characters)")
    lengths = [len(r) for r in ds["response"]]
    print(f"  Min    : {min(lengths)}")
    print(f"  Max    : {max(lengths)}")
    print(f"  Mean   : {statistics.mean(lengths):.0f}")
    print(f"  Median : {statistics.median(lengths):.0f}")

    # Bucket distribution
    buckets = {"0-100": 0, "101-300": 0, "301-600": 0, "601+": 0}
    for l in lengths:
        if l <= 100:
            buckets["0-100"] += 1
        elif l <= 300:
            buckets["101-300"] += 1
        elif l <= 600:
            buckets["301-600"] += 1
        else:
            buckets["601+"] += 1
    print(f"\n  Length buckets:")
    for bucket, count in buckets.items():
        pct = count / len(lengths) * 100
        print(f"    {bucket:<10} {count:>5} examples  ({pct:.1f}%)")

    # ── 5. Placeholder detection ───────────────────────────────
    print(f"\n[5] PLACEHOLDER DETECTION")
    placeholder_count = 0
    for row in ds:
        if "{{" in row["instruction"] or "{{" in row["response"]:
            placeholder_count += 1
    pct = placeholder_count / len(ds) * 100
    print(f"  Examples containing {{{{...}}}} placeholders: {placeholder_count} ({pct:.1f}%)")

    # ── 6. Flags breakdown ─────────────────────────────────────
    print(f"\n[6] FLAGS BREAKDOWN")
    flags = ds["flags"]
    flag_counts: dict[str, int] = {}
    for f in flags:
        flag_counts[f] = flag_counts.get(f, 0) + 1
    for flag, count in sorted(flag_counts.items(), key=lambda x: -x[1])[:15]:
        print(f"  '{flag}'  →  {count} examples")

    # ── 7. Duplicate detection ─────────────────────────────────
    print(f"\n[7] DUPLICATE DETECTION")
    instructions = ds["instruction"]
    unique_instructions = set(instructions)
    duplicates = len(instructions) - len(unique_instructions)
    print(f"  Total instructions  : {len(instructions)}")
    print(f"  Unique instructions : {len(unique_instructions)}")
    print(f"  Exact duplicates    : {duplicates}")

    # ── 8. Sample of short vs long responses ──────────────────
    print(f"\n[8] SAMPLE: SHORTEST RESPONSES")
    sorted_by_len = sorted(
        zip(ds["instruction"], ds["response"]),
        key=lambda x: len(x[1])
    )
    for instruction, response in sorted_by_len[:3]:
        print(f"\n  Q: {instruction[:80]}")
        print(f"  A: {response[:200]}")

    print(f"\n[9] SAMPLE: LONGEST RESPONSES")
    for instruction, response in sorted_by_len[-3:]:
        print(f"\n  Q: {instruction[:80]}")
        print(f"  A: {response[:200]}...")


if __name__ == "__main__":
    analyze_dataset("bitext/Bitext-customer-support-llm-chatbot-training-dataset")

    # Preview Dialogstudio
    ds = load_dataset("Salesforce/dialogstudio", "MULTIWOZ2_2", split="train")
    print(f"\n{'='*60}")
    print(f"Dataset : Salesforce/dialogstudio")
    print(f"Config  : MULTIWOZ2_2")
    print(f"{'='*60}")
    print(f"Total examples : {len(ds)}")
    print(f"Columns        : {ds.column_names}")
    for i in range(min(2, len(ds))):
        print(f"\n[Example {i+1}]")
        for col in ds.column_names:
            value = ds[i][col]
            if isinstance(value, (list, dict)):
                value = json.dumps(value, indent=4)
                if len(value) > 800:
                    value = value[:800] + "\n... (truncated)"
            elif isinstance(value, str) and len(value) > 400:
                value = value[:400] + "... (truncated)"
            print(f"  {col}:")
            print(f"    {value}")