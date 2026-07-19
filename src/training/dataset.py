"""
Phase 7: Prompt Formatting and Tokenization
Goal: Convert train_final.json into tokenized sequences with
      correct loss masks for Qwen 2.5 fine-tuning.

Key concepts implemented here:
    - ChatML formatting for Qwen 2.5
    - Loss masking: -100 for system/user tokens, token_id for assistant tokens
    - Sequence length validation
    - HuggingFace Dataset creation for the training pipeline
"""

import json
from pathlib import Path
from typing import Any

from transformers import AutoTokenizer
from datasets import Dataset


# ── Constants ──────────────────────────────────────────────────────────────────

TRAIN_DATA_PATH = Path("data/processed/train_final.json")
MODEL_NAME      = "Qwen/Qwen2.5-0.5B-Instruct"  # Smallest Qwen 2.5 for testing
MAX_LENGTH      = 512                             # Max tokens per example


# ── Load tokenizer ─────────────────────────────────────────────────────────────

def load_tokenizer(model_name: str) -> AutoTokenizer:
    """
    Load the Qwen 2.5 tokenizer from HuggingFace.
    We use the tokenizer only here — the model is loaded separately
    during training to keep memory management clean.

    Args:
        model_name: HuggingFace model identifier

    Returns:
        Configured AutoTokenizer instance
    """
    print(f"Loading tokenizer: {model_name}")
    tokenizer = AutoTokenizer.from_pretrained(
        model_name,
        trust_remote_code=True,
    )

    # Qwen 2.5 uses the eos token as pad token
    # This is standard practice for decoder-only models
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    print(f"Vocabulary size  : {tokenizer.vocab_size}")
    print(f"Pad token        : {tokenizer.pad_token}")
    print(f"EOS token        : {tokenizer.eos_token}")
    return tokenizer


# ── ChatML formatting ──────────────────────────────────────────────────────────

def format_as_chatml(messages: list[dict]) -> str:
    """
    Convert a list of messages into Qwen 2.5 ChatML format string.

    The output looks like:
        <|im_start|>system
        You are...<|im_end|>
        <|im_start|>user
        Hello<|im_end|>
        <|im_start|>assistant
        Hi there!<|im_end|>

    Args:
        messages: List of {"role": ..., "content": ...} dicts

    Returns:
        Formatted ChatML string
    """
    formatted = ""
    for msg in messages:
        role    = msg["role"]
        content = msg["content"]
        formatted += f"<|im_start|>{role}\n{content}<|im_end|>\n"
    return formatted


# ── Loss mask creation ─────────────────────────────────────────────────────────

def create_loss_mask(
    messages: list[dict],
    tokenizer: AutoTokenizer,
    max_length: int,
) -> dict[str, list[int]]:
    """
    Tokenize a conversation and create a loss mask that marks only
    assistant tokens for loss calculation.

    System and user tokens get label -100 (ignored by loss function).
    Assistant tokens get their actual token ID as the label.

    Args:
        messages:   List of role/content message dicts
        tokenizer:  Loaded tokenizer instance
        max_length: Maximum sequence length in tokens

    Returns:
        Dict with input_ids, attention_mask, and labels lists
    """
    input_ids: list[int] = []
    labels:    list[int] = []

    for msg in messages:
        role    = msg["role"]
        content = msg["content"]

        # Format this single turn as ChatML
        turn_text = f"<|im_start|>{role}\n{content}<|im_end|>\n"

        # Tokenize this turn (no special tokens — we handle them manually)
        turn_ids = tokenizer.encode(turn_text, add_special_tokens=False)

        # Extend the full sequence
        input_ids.extend(turn_ids)

        # For assistant turns: use real token IDs as labels
        # For system/user turns: use -100 to mask from loss
        if role == "assistant":
            labels.extend(turn_ids)
        else:
            labels.extend([-100] * len(turn_ids))

    # Truncate to max_length if needed
    input_ids = input_ids[:max_length]
    labels    = labels[:max_length]

    # Create attention mask: 1 for all real tokens
    attention_mask = [1] * len(input_ids)

    return {
        "input_ids":      input_ids,
        "attention_mask": attention_mask,
        "labels":         labels,
    }


# ── Dataset builder ────────────────────────────────────────────────────────────

def build_training_dataset(
    data_path: Path,
    tokenizer: AutoTokenizer,
    max_length: int = MAX_LENGTH,
) -> Dataset:
    """
    Load train_final.json, tokenize every example, apply loss masks,
    and return a HuggingFace Dataset ready for the training loop.

    Args:
        data_path:  Path to train_final.json
        tokenizer:  Loaded tokenizer instance
        max_length: Maximum sequence length

    Returns:
        HuggingFace Dataset with input_ids, attention_mask, labels
    """
    print(f"\nBuilding training dataset from: {data_path}")

    with open(data_path, "r", encoding="utf-8") as f:
        raw_data: list[dict] = json.load(f)

    print(f"Loaded {len(raw_data)} examples")

    tokenized: list[dict[str, list[int]]] = []
    skipped = 0
    too_long = 0

    for example in raw_data:
        messages = example.get("messages", [])

        if not messages:
            skipped += 1
            continue

        result = create_loss_mask(messages, tokenizer, max_length)

        # Skip examples that are entirely masked (no assistant tokens)
        if all(label == -100 for label in result["labels"]):
            skipped += 1
            continue

        # Track examples that were truncated
        full_ids = tokenizer.encode(
            format_as_chatml(messages),
            add_special_tokens=False
        )
        if len(full_ids) > max_length:
            too_long += 1

        tokenized.append(result)

    print(f"Tokenized        : {len(tokenized)}")
    print(f"Skipped          : {skipped}")
    print(f"Truncated        : {too_long}")

    return Dataset.from_list(tokenized)


# ── Verification ───────────────────────────────────────────────────────────────

def verify_single_example(
    example: dict,
    tokenizer: AutoTokenizer,
) -> None:
    """
    Print a human-readable breakdown of one tokenized example.
    Shows which tokens are masked and which contribute to loss.

    Args:
        example:   A single dict from train_final.json
        tokenizer: Loaded tokenizer instance
    """
    print(f"\n{'='*60}")
    print("SINGLE EXAMPLE VERIFICATION")
    print(f"{'='*60}")

    messages = example["messages"]
    result   = create_loss_mask(messages, tokenizer, MAX_LENGTH)

    input_ids = result["input_ids"]
    labels    = result["labels"]

    print(f"Total tokens     : {len(input_ids)}")
    active = sum(1 for l in labels if l != -100)
    masked = sum(1 for l in labels if l == -100)
    print(f"Active labels    : {active}  (assistant tokens — loss calculated)")
    print(f"Masked labels    : {masked}  (system/user tokens — ignored)")
    print(f"Loss coverage    : {active/len(labels)*100:.1f}%")

    print(f"\nFirst 5 tokens decoded:")
    for i in range(min(5, len(input_ids))):
        token_str = tokenizer.decode([input_ids[i]])
        label_str = str(labels[i]) if labels[i] != -100 else "MASKED"
        print(f"  [{i}] token={repr(token_str):<15} label={label_str}")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Load tokenizer
    tokenizer = load_tokenizer(MODEL_NAME)

    # Verify formatting on one example
    with open(TRAIN_DATA_PATH, "r", encoding="utf-8") as f:
        sample = json.load(f)[0]

    verify_single_example(sample, tokenizer)

    # Build full dataset
    dataset = build_training_dataset(TRAIN_DATA_PATH, tokenizer)

    print(f"\n{'='*60}")
    print("DATASET READY")
    print(f"{'='*60}")
    print(f"Total examples   : {len(dataset)}")
    print(f"Features         : {dataset.features}")
    print(f"\nFirst example keys: {list(dataset[0].keys())}")
    print(f"input_ids length : {len(dataset[0]['input_ids'])}")
    print(f"labels length    : {len(dataset[0]['labels'])}")