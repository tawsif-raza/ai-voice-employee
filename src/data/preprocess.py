"""
Phase 4: Preprocessing
Goal: Convert both cleaned datasets into unified ChatML-ready format.

Every output example will have this structure:
    {
        "messages": [
            {"role": "system",    "content": "<system prompt>"},
            {"role": "user",      "content": "<user message>"},
            {"role": "assistant", "content": "<assistant response>"},
            ... (more turns for multi-turn conversations)
        ]
    }

This format maps directly to Qwen 2.5's ChatML template.
"""

import json
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────────

BITEXT_INPUT_PATH     = Path("data/cleaned/bitext_cleaned.json")
ULTRACHAT_INPUT_PATH  = Path("data/cleaned/ultrachat_cleaned.json")
BITEXT_OUTPUT_PATH    = Path("data/processed/bitext_processed.json")
ULTRACHAT_OUTPUT_PATH = Path("data/processed/ultrachat_processed.json")

# This system prompt will be injected into every training example.
# It defines the model's persona and behavioral constraints.
# Every word here shapes how the model behaves at inference time.
SYSTEM_PROMPT = (
    "You are a helpful, professional customer support voice assistant. "
    "Keep your responses brief, clear, and conversational. "
    "Never use bullet points or numbered lists. "
    "Speak naturally as if on a phone call. "
    "If you cannot help, offer to connect the customer to a human agent."
)


# ── Bitext preprocessing ───────────────────────────────────────────────────────

def preprocess_bitext(input_path: Path, output_path: Path) -> None:
    """
    Convert Bitext flat format into unified chat format.
    Adds system prompt and wraps instruction/response as user/assistant turn.

    Args:
        input_path:  Path to bitext_cleaned.json
        output_path: Path to save bitext_processed.json
    """
    print(f"\n{'='*60}")
    print("PREPROCESSING — Bitext")
    print(f"{'='*60}")

    # Load cleaned data
    with open(input_path, "r", encoding="utf-8") as f:
        raw_data: list[dict] = json.load(f)
    print(f"Loaded : {len(raw_data)} examples")

    processed = []
    skipped   = 0

    for row in raw_data:
        instruction = row.get("instruction", "").strip()
        response    = row.get("response", "").strip()

        # Skip if either field is empty after stripping
        if not instruction or not response:
            skipped += 1
            continue

        example = {
            "messages": [
                {"role": "system",    "content": SYSTEM_PROMPT},
                {"role": "user",      "content": instruction},
                {"role": "assistant", "content": response},
            ]
        }
        processed.append(example)

    # Save
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(processed, f, indent=2, ensure_ascii=False)

    print(f"Skipped : {skipped} empty examples")
    print(f"Saved   : {len(processed)} examples → {output_path}")


# ── UltraChat preprocessing ────────────────────────────────────────────────────

def preprocess_ultrachat(input_path: Path, output_path: Path) -> None:
    """
    Convert UltraChat chat format into unified format.
    Injects system prompt at the start of every conversation.
    Validates that messages alternate correctly between user and assistant.

    Args:
        input_path:  Path to ultrachat_cleaned.json
        output_path: Path to save ultrachat_processed.json
    """
    print(f"\n{'='*60}")
    print("PREPROCESSING — UltraChat")
    print(f"{'='*60}")

    with open(input_path, "r", encoding="utf-8") as f:
        raw_data: list[dict] = json.load(f)
    print(f"Loaded : {len(raw_data)} conversations")

    processed = []
    skipped   = 0

    for row in raw_data:
        messages: list[dict] = row.get("messages", [])

        # Validate: must have at least one user and one assistant message
        roles = [m["role"] for m in messages]
        if "user" not in roles or "assistant" not in roles:
            skipped += 1
            continue

        # Validate: first message must be from user
        if messages[0]["role"] != "user":
            skipped += 1
            continue

        # Inject system prompt at the beginning
        full_messages = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ] + messages

        processed.append({"messages": full_messages})

    # Save
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(processed, f, indent=2, ensure_ascii=False)

    print(f"Skipped : {skipped} invalid conversations")
    print(f"Saved   : {len(processed)} conversations → {output_path}")


# ── Verification ───────────────────────────────────────────────────────────────

def verify_output(path: Path, n: int = 2) -> None:
    """
    Print the first n examples from a processed file to verify structure.

    Args:
        path: Path to a processed JSON file
        n:    Number of examples to print
    """
    print(f"\n{'='*60}")
    print(f"VERIFICATION — {path.name}")
    print(f"{'='*60}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    print(f"Total examples: {len(data)}\n")

    for i, example in enumerate(data[:n]):
        print(f"[Example {i+1}]")
        for msg in example["messages"]:
            role    = msg["role"].upper()
            content = msg["content"]
            if len(content) > 120:
                content = content[:120] + "..."
            print(f"  {role}: {content}")
        print()


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Process both datasets
    preprocess_bitext(BITEXT_INPUT_PATH, BITEXT_OUTPUT_PATH)
    preprocess_ultrachat(ULTRACHAT_INPUT_PATH, ULTRACHAT_OUTPUT_PATH)

    # Verify both outputs look correct
    verify_output(BITEXT_OUTPUT_PATH)
    verify_output(ULTRACHAT_OUTPUT_PATH)
