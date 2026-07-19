"""
Phase 5: Dataset Merging (v2)
Goal: Combine all three datasets into one final training file.

Sources:
    - Bitext customer support (single-turn, high domain quality)
    - UltraChat filtered (multi-turn, general support)
    - Custom examples (hand-crafted, highest signal)
"""

import json
import random
from pathlib import Path


# ── Constants ──────────────────────────────────────────────────────────────────

BITEXT_PATH    = Path("data/processed/bitext_processed.json")
ULTRACHAT_PATH = Path("data/processed/ultrachat_processed.json")
CUSTOM_PATH    = Path("data/custom/custom_examples.json")
OUTPUT_PATH    = Path("data/processed/train_final.json")
RANDOM_SEED    = 42


# ── Validation ─────────────────────────────────────────────────────────────────

def is_valid_example(example: dict) -> bool:
    """
    Validate that an example has the correct structure for training.

    Args:
        example: A single training example dict

    Returns:
        True if the example passes all validation checks
    """
    if "messages" not in example:
        return False

    messages = example["messages"]

    if not isinstance(messages, list) or len(messages) < 3:
        return False

    roles = [m.get("role") for m in messages]

    if roles[0] != "system":
        return False

    if "user" not in roles:
        return False

    if "assistant" not in roles:
        return False

    for msg in messages:
        if not msg.get("content", "").strip():
            return False

    return True


# ── Main merge pipeline ────────────────────────────────────────────────────────

def merge_datasets(
    bitext_path: Path,
    ultrachat_path: Path,
    custom_path: Path,
    output_path: Path,
    seed: int = RANDOM_SEED,
) -> None:
    """
    Merge, validate, shuffle, and save the final training dataset.

    Args:
        bitext_path:    Path to bitext_processed.json
        ultrachat_path: Path to ultrachat_processed.json
        custom_path:    Path to custom_examples.json
        output_path:    Path to save train_final.json
        seed:           Random seed for reproducible shuffling
    """
    print(f"\n{'='*60}")
    print(f"MERGE PIPELINE — v2 (all three sources)")
    print(f"{'='*60}\n")

    # ── Load all three datasets ────────────────────────────────
    print("[1/5] Loading all datasets...")

    with open(bitext_path, "r", encoding="utf-8") as f:
        bitext_data: list[dict] = json.load(f)

    with open(ultrachat_path, "r", encoding="utf-8") as f:
        ultrachat_data: list[dict] = json.load(f)

    with open(custom_path, "r", encoding="utf-8") as f:
        custom_data: list[dict] = json.load(f)

    print(f"      Bitext    : {len(bitext_data)} examples")
    print(f"      UltraChat : {len(ultrachat_data)} examples")
    print(f"      Custom    : {len(custom_data)} examples")
    print(f"      Combined  : {len(bitext_data) + len(ultrachat_data) + len(custom_data)} examples\n")

    # ── Combine ────────────────────────────────────────────────
    print("[2/5] Combining datasets...")
    combined = bitext_data + ultrachat_data + custom_data
    print(f"      Total before validation: {len(combined)}\n")

    # ── Validate ───────────────────────────────────────────────
    print("[3/5] Validating all examples...")
    valid   = []
    invalid = 0

    for example in combined:
        if is_valid_example(example):
            valid.append(example)
        else:
            invalid += 1

    print(f"      Valid   : {len(valid)}")
    print(f"      Invalid : {invalid}\n")

    # ── Shuffle ────────────────────────────────────────────────
    print(f"[4/5] Shuffling with seed {seed}...")
    random.seed(seed)
    random.shuffle(valid)
    print(f"      Shuffled {len(valid)} examples\n")

    # ── Save ───────────────────────────────────────────────────
    print("[5/5] Saving final training file...")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(valid, f, indent=2, ensure_ascii=False)
    print(f"      Saved to: {output_path}\n")

    # ── Final report ───────────────────────────────────────────
    total = len(valid)
    bitext_pct    = len(bitext_data)    / total * 100
    ultrachat_pct = len(ultrachat_data) / total * 100
    custom_pct    = len(custom_data)    / total * 100

    print("="*60)
    print("FINAL MERGE REPORT")
    print("="*60)
    print(f"  Bitext examples    : {len(bitext_data):<6} ({bitext_pct:.1f}%)")
    print(f"  UltraChat examples : {len(ultrachat_data):<6} ({ultrachat_pct:.1f}%)")
    print(f"  Custom examples    : {len(custom_data):<6} ({custom_pct:.1f}%)")
    print(f"  Invalid removed    : {invalid}")
    print(f"  Final total        : {total}")
    print(f"  Saved to           : {output_path}")
    print("="*60)

    # ── Spot check ─────────────────────────────────────────────
    print("\nSPOT CHECK — 3 random examples after shuffle:\n")
    spot_indices = random.sample(range(len(valid)), 3)
    for i in spot_indices:
        example = valid[i]
        print(f"[Example at index {i}]")
        for msg in example["messages"]:
            role    = msg["role"].upper()
            content = msg["content"][:100]
            print(f"  {role}: {content}")
        print()

def merge_bitext_and_custom(
    bitext_path: Path,
    custom_path: Path,
    output_path: Path,
    seed: int = RANDOM_SEED,
) -> None:
    """
    Final merge using only Bitext and Custom data.
    UltraChat dropped due to persistent off-domain noise after
    multiple filtering iterations — quality over quantity.

    Args:
        bitext_path: Path to bitext_processed.json
        custom_path: Path to custom_examples.json
        output_path: Path to save train_final.json
        seed:        Random seed for reproducible shuffling
    """
    print(f"\n{'='*60}")
    print(f"MERGE PIPELINE — Final (Bitext + Custom only)")
    print(f"{'='*60}\n")

    print("[1/5] Loading datasets...")
    with open(bitext_path, "r", encoding="utf-8") as f:
        bitext_data: list[dict] = json.load(f)

    with open(custom_path, "r", encoding="utf-8") as f:
        custom_data: list[dict] = json.load(f)

    print(f"      Bitext  : {len(bitext_data)} examples")
    print(f"      Custom  : {len(custom_data)} examples")
    print(f"      Combined: {len(bitext_data) + len(custom_data)} examples\n")

    print("[2/5] Combining...")
    combined = bitext_data + custom_data
    print(f"      Total before validation: {len(combined)}\n")

    print("[3/5] Validating...")
    valid   = []
    invalid = 0
    for example in combined:
        if is_valid_example(example):
            valid.append(example)
        else:
            invalid += 1
    print(f"      Valid   : {len(valid)}")
    print(f"      Invalid : {invalid}\n")

    print(f"[4/5] Shuffling with seed {seed}...")
    random.seed(seed)
    random.shuffle(valid)
    print(f"      Shuffled {len(valid)} examples\n")

    print("[5/5] Saving...")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(valid, f, indent=2, ensure_ascii=False)
    print(f"      Saved to: {output_path}\n")

    total         = len(valid)
    bitext_pct    = len(bitext_data) / total * 100
    custom_pct    = len(custom_data) / total * 100

    print("="*60)
    print("FINAL MERGE REPORT")
    print("="*60)
    print(f"  Bitext examples  : {len(bitext_data):<6} ({bitext_pct:.1f}%)")
    print(f"  Custom examples  : {len(custom_data):<6} ({custom_pct:.1f}%)")
    print(f"  Invalid removed  : {invalid}")
    print(f"  Final total      : {total}")
    print(f"  Saved to         : {output_path}")
    print("="*60)

    print("\nSPOT CHECK — 3 random examples:\n")
    for i in random.sample(range(len(valid)), 3):
        example = valid[i]
        print(f"[Index {i}]")
        for msg in example["messages"]:
            print(f"  {msg['role'].upper()}: {msg['content'][:100]}")
        print()
# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    merge_bitext_and_custom(BITEXT_PATH, CUSTOM_PATH, OUTPUT_PATH)