"""
Phase 3: UltraChat Dataset Cleaning (v2)
Goal: Filter UltraChat 200k down to customer-support relevant
      multi-turn conversations, removing bullet points and
      applying stricter domain filtering.

Cleaning steps:
    1. Strict domain filter (2+ keyword matches)
    2. Minimum turn count filter
    3. Response length filter
    4. Bullet point and numbered list filter
    5. Normalize whitespace
    6. Save in chat format
"""

import re
import json
from datasets import load_dataset
from pathlib import Path


# ── Constants ──────────────────────────────────────────────────────────────────

DATASET_NAME       = "HuggingFaceH4/ultrachat_200k"
OUTPUT_PATH        = Path("data/cleaned/ultrachat_cleaned.json")
MAX_RESPONSE_CHARS = 300
MIN_TURNS          = 2


# ── Domain keywords ────────────────────────────────────────────────────────────

DOMAIN_KEYWORDS: list[str] = [
    "customer", "support", "service", "help", "assist",
    "order", "cancel", "refund", "return", "complaint",
    "account", "payment", "invoice", "subscription", "billing",
    "appointment", "booking", "schedule", "reservation",
    "delivery", "shipping", "track", "package",
    "password", "login", "access", "reset",
    "warranty", "repair", "replace", "exchange",
    "charge", "fee", "price", "cost", "quote",
    "policy", "terms", "contract", "agreement",
    "feedback", "review", "rating", "experience",
    "problem", "issue", "error", "broken", "fix",
    "contact", "phone", "email", "reach", "speak",
    "human", "agent", "representative", "manager",
    "upgrade", "downgrade", "plan", "tier",
]


# ── Helper functions ───────────────────────────────────────────────────────────

def is_strongly_domain_relevant(prompt: str) -> bool:
    """
    Return True if the prompt contains at least 2 domain keywords.
    Stricter than the original single-keyword check to reduce
    off-domain examples that slip through on weak matches.

    Args:
        prompt: The opening user message of the conversation

    Returns:
        True if 2 or more domain keywords found
    """
    prompt_lower = prompt.lower()
    matches = 0
    for keyword in DOMAIN_KEYWORDS:
        if re.search(rf"\b{keyword}\b", prompt_lower):
            matches += 1
        if matches >= 2:
            return True
    return False


def has_minimum_turns(messages: list[dict]) -> bool:
    """
    Return True if the conversation has at least MIN_TURNS exchanges.
    Each exchange = 1 user message + 1 assistant message = 2 items.

    Args:
        messages: List of role/content dicts

    Returns:
        True if turn count meets minimum
    """
    return len(messages) >= MIN_TURNS * 2


def get_first_assistant_response(messages: list[dict]) -> str:
    """
    Extract the first assistant response from a conversation.

    Args:
        messages: List of role/content dicts

    Returns:
        Content of the first assistant message, or empty string
    """
    for msg in messages:
        if msg.get("role") == "assistant":
            return msg.get("content", "")
    return ""


def is_response_too_long(response: str) -> bool:
    """
    Return True if response exceeds voice-safe character limit.

    Args:
        response: Assistant response string

    Returns:
        True if longer than MAX_RESPONSE_CHARS
    """
    return len(response) > MAX_RESPONSE_CHARS


def has_bullet_points(messages: list[dict]) -> bool:
    """
    Return True if any assistant message contains bullet points
    or numbered lists. These patterns are not voice-friendly and
    contradict our system prompt instructions.

    Args:
        messages: List of role/content dicts

    Returns:
        True if list formatting detected in any assistant turn
    """
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        if re.search(r"^\s*\d+[\.\)]", content, re.MULTILINE):
            return True
        if re.search(r"^\s*[-•*]", content, re.MULTILINE):
            return True
    return False


def normalize_messages(messages: list[dict]) -> list[dict]:
    """
    Normalize whitespace in all message content fields.

    Args:
        messages: List of role/content dicts

    Returns:
        New list with cleaned content strings
    """
    cleaned = []
    for msg in messages:
        cleaned.append({
            "role":    msg["role"],
            "content": re.sub(r"\s+", " ", msg["content"]).strip()
        })
    return cleaned

def is_document_qa(prompt: str) -> bool:
    """
    Return True if the prompt is a document Q&A style conversation
    rather than a genuine customer support interaction.
    These prompts paste a block of text and ask questions about it.

    Args:
        prompt: The opening user message

    Returns:
        True if the prompt appears to be document Q&A
    """
    doc_qa_signals: list[str] = [
        "here is a piece of text",
        "here's a piece of text",
        "based on the following",
        "based on the text",
        "read the following",
        "given the following text",
        "refer to the following",
        "according to the passage",
    ]
    prompt_lower = prompt.lower()
    for signal in doc_qa_signals:
        if signal in prompt_lower:
            return True
    return False


# ── Main cleaning pipeline ─────────────────────────────────────────────────────

def clean_ultrachat(dataset_name: str, output_path: Path) -> None:
    """
    Run the full cleaning pipeline on UltraChat 200k.

    Args:
        dataset_name: HuggingFace dataset identifier
        output_path:  Where to save the cleaned JSON file
    """
    print(f"\n{'='*60}")
    print(f"CLEANING PIPELINE — UltraChat 200k (v2)")
    print(f"Output : {output_path}")
    print(f"{'='*60}\n")

    # ── Load ───────────────────────────────────────────────────
    print("[1/7] Loading dataset...")
    ds = load_dataset(dataset_name, split="train_sft")
    total_start = len(ds)
    print(f"      Loaded {total_start} examples\n")

    removed_domain   = 0
    removed_turns    = 0
    removed_too_long = 0
    removed_bullets  = 0

    # ── Step 1: Strict domain filter ──────────────────────────
    print("[2/7] Filtering by strict domain relevance (2+ keywords)...")
    after_domain = []
    for row in ds:
        if not is_strongly_domain_relevant(row["prompt"]):
            removed_domain += 1
        else:
            after_domain.append(row)
    print(f"      Removed : {removed_domain}")
    print(f"      Remaining: {len(after_domain)}\n")

    # ── Step 1b: Remove document Q&A conversations ─────────────
    print("[2b/7] Removing document Q&A style conversations...")
    removed_doc_qa = 0
    after_doc_qa = []
    for row in after_domain:
        if is_document_qa(row["prompt"]):
            removed_doc_qa += 1
        else:
            after_doc_qa.append(row)
    print(f"      Removed : {removed_doc_qa}")
    print(f"      Remaining: {len(after_doc_qa)}\n")

    # ── Step 2: Minimum turns filter ──────────────────────────
    print("[3/7] Filtering by minimum turn count...")
    after_turns = []
    for row in after_doc_qa:  # ← change this line
        if not has_minimum_turns(row["messages"]):
            removed_turns += 1
        else:
            after_turns.append(row)
    print(f"      Removed : {removed_turns}")
    print(f"      Remaining: {len(after_turns)}\n")

    # ── Step 3: Response length filter ────────────────────────
    print("[4/7] Filtering by first assistant response length...")
    after_length = []
    for row in after_turns:
        first_response = get_first_assistant_response(row["messages"])
        if is_response_too_long(first_response):
            removed_too_long += 1
        else:
            after_length.append(row)
    print(f"      Removed : {removed_too_long}")
    print(f"      Remaining: {len(after_length)}\n")

    # ── Step 4: Bullet point filter ───────────────────────────
    print("[5/7] Removing conversations with bullet points or numbered lists...")
    after_bullets = []
    for row in after_length:
        if has_bullet_points(row["messages"]):
            removed_bullets += 1
        else:
            after_bullets.append(row)
    print(f"      Removed : {removed_bullets}")
    print(f"      Remaining: {len(after_bullets)}\n")

    # ── Step 5: Normalize whitespace ──────────────────────────
    print("[6/7] Normalizing whitespace...")
    cleaned_examples = []
    for row in after_bullets:
        cleaned_examples.append({
            "messages": normalize_messages(row["messages"])
        })
    print(f"      Done. {len(cleaned_examples)} conversations normalized.\n")

    # ── Step 6: Save ───────────────────────────────────────────
    print("[7/7] Saving cleaned dataset...")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(cleaned_examples, f, indent=2, ensure_ascii=False)
    print(f"      Saved to: {output_path}\n")
    print(f"  Document Q&A filter : -{removed_doc_qa}")

    # ── Final report ───────────────────────────────────────────
    total_end     = len(cleaned_examples)
    total_removed = total_start - total_end
    retention     = total_end / total_start * 100

    print("="*60)
    print("CLEANING REPORT — UltraChat v2")
    print("="*60)
    print(f"  Started with        : {total_start}")
    print(f"  Domain filter       : -{removed_domain}")
    print(f"  Turn count filter   : -{removed_turns}")
    print(f"  Response too long   : -{removed_too_long}")
    print(f"  Bullet point filter : -{removed_bullets}")
    print(f"  Total removed       : {total_removed}")
    print(f"  Final clean convos  : {total_end}")
    print(f"  Retention rate      : {retention:.1f}%")
    print("="*60)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    clean_ultrachat(DATASET_NAME, OUTPUT_PATH)