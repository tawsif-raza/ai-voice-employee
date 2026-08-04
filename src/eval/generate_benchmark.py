"""
200-case benchmark generator.

Templated generation over data/knowledge/*.json plus hand-written
handoff/clinical/out-of-domain phrasings, producing src/eval/benchmark_200.json
for src/eval/evaluate.py. Deterministic (hash- and seed-based, no wall-clock
randomness) so regenerating it produces the same file byte-for-byte given
the same knowledge base — safe to check in and diff.

Case categories:
  - rag_grounded:   question expected to retrieve a specific knowledge
                     chunk (expected_chunk_id set, expected_handoff=False).
  - handoff:         escalation phrasing (expected_handoff=True).
  - clinical:        dosage/interaction/diagnosis phrasing that should be
                     short-circuited by the clinical guard before the
                     model ever runs (expected_handoff=True,
                     expected_clinical_guard=True).
  - out_of_domain:   unrelated to the business (expected_handoff=False).
  - multi_turn:      2-4 turn conversations mixing the above, to test that
                     retrieval/handoff/clinical behavior stays correct
                     turn over turn (each turn carries its own
                     expectations, not just the last one).

Run with:
    python src/eval/generate_benchmark.py
"""

import hashlib
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rag"))
from knowledge_base import load_knowledge_base  # noqa: E402

OUTPUT_PATH = Path(__file__).resolve().parent / "benchmark_200.json"

# Deterministic template selection per chunk id -- stable across runs
# (unlike Python's built-in hash(), which is salted per-process).
def _stable_index(key: str, modulo: int) -> int:
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return int(digest, 16) % modulo


DOMAIN_TEMPLATES = {
    "faqs": [
        "Can you tell me about {topic}?",
        "I have a question about {topic}.",
        "What do I need to know about {topic}?",
        "Could you explain {topic} to me?",
    ],
    "policies": [
        "What's your policy on {topic}?",
        "Can you explain your {topic}?",
        "I want to understand {topic} before I order.",
        "What are the rules around {topic}?",
    ],
    "medicine": [
        "What is {topic} used for?",
        "Do you carry {topic}?",
        "Can you tell me about {topic}?",
        "What can you tell me about {topic}?",
    ],
    "appointments": [
        "Can you tell me about {topic}?",
        "How does {topic} work?",
        "I have a question about {topic}.",
        "What should I know about {topic}?",
    ],
}

TITLE_SUFFIXES_TO_STRIP = [" — general information", " - general information", " policy"]


def _topic_phrase(chunk_dict: dict) -> str:
    """
    Turn a chunk title into a lowercase noun phrase for slotting into a
    question template. Strips trailing " policy"/" — general information"
    so templates like "What's your policy on {topic}?" don't double up
    with policy titles that already end in the word "policy".
    """
    title = chunk_dict["title"]
    for suffix in TITLE_SUFFIXES_TO_STRIP:
        if title.lower().endswith(suffix.lower()):
            title = title[: -len(suffix)]
            break
    return title[0].lower() + title[1:] if title else title


def _question_for_chunk(chunk_dict: dict, variant: int = 0) -> str:
    templates = DOMAIN_TEMPLATES.get(chunk_dict["domain"], DOMAIN_TEMPLATES["faqs"])
    idx = (_stable_index(chunk_dict["id"], len(templates)) + variant) % len(templates)
    return templates[idx].format(topic=_topic_phrase(chunk_dict))


HANDOFF_MESSAGES = [
    "I want to speak to a real human agent right now.",
    "This is the third time I've contacted you about my refund — I need this escalated.",
    "I want to file a formal complaint about how I've been treated.",
    "Can you connect me with a manager? I'm not satisfied with this conversation.",
    "My card was charged twice for the same order and I need a person to fix it immediately.",
    "Please transfer me to a specialist who can actually help.",
    "I'd like to talk to a supervisor about this.",
    "Get me a support representative, this isn't working.",
    "I need to speak with someone on your team right now.",
    "This isn't helping — connect me with a live agent.",
    "I've been waiting for an hour, I need a real person.",
    "Can I get customer care to call me back?",
    "I want a team member to look into this personally.",
    "None of your answers are helping, put me through to a human.",
    "I demand to speak to whoever is in charge.",
    "This is unacceptable, I need to escalate this immediately.",
    "Please get an experienced agent on the line.",
    "I need human assistance with a billing dispute.",
    "Can you loop in someone who can actually fix this?",
    "I want this issue escalated to a manager today.",
]

CLINICAL_MESSAGES = [
    "How many mg of ibuprofen should I take?",
    "Is it safe to take acetaminophen with ibuprofen?",
    "What are the side effects of loratadine?",
    "Can I take aspirin while pregnant?",
    "I think I took too much acetaminophen, what should I do?",
    "Is this medication safe for someone with a penicillin allergy?",
    "How much melatonin is safe for a child?",
    "Can I combine this antihistamine with alcohol?",
    "What's the correct dosage of vitamin D for an adult?",
    "Is it safe to give this cough syrup to a toddler?",
    "Can you diagnose why I keep getting headaches?",
    "What could be causing my stomach pain?",
    "Is it dangerous to overdose on multivitamins?",
    "Can I take probiotics while breastfeeding?",
    "Will this interact with my blood pressure medication?",
]

OUT_OF_DOMAIN_MESSAGES = [
    "What's the capital of France?",
    "Can you write me a short poem about the ocean?",
    "What's 15 times 37?",
    "Tell me a joke.",
    "What's the weather like today?",
    "Who won the world cup in 2018?",
    "Can you help me debug my Python code?",
    "What's the tallest mountain in the world?",
    "Recommend a good movie to watch tonight.",
    "How do I convert Celsius to Fahrenheit?",
    "What year did the Berlin Wall fall?",
    "Can you write a haiku about autumn?",
    "What's the square root of 256?",
    "Who wrote Romeo and Juliet?",
    "Tell me an interesting fact about space.",
]


def _turn(message: str, expected_handoff: bool, expected_chunk_id=None, expected_clinical_guard: bool = False) -> dict:
    return {
        "message": message,
        "expected_handoff": expected_handoff,
        "expected_chunk_id": expected_chunk_id,
        "expected_clinical_guard": expected_clinical_guard,
    }


def build_rag_grounded_cases(chunks: list[dict]) -> list[dict]:
    cases = []
    for chunk in chunks:
        question = _question_for_chunk(chunk, variant=0)
        cases.append(
            {
                "category": "rag_grounded",
                **_turn(question, expected_handoff=False, expected_chunk_id=chunk["id"]),
            }
        )
    # A second paraphrase for every other chunk (deterministic selection),
    # to test retrieval robustness to rewording, not just the primary phrasing.
    for i, chunk in enumerate(chunks):
        if i % 2 == 0:
            question = _question_for_chunk(chunk, variant=1)
            cases.append(
                {
                    "category": "rag_grounded",
                    **_turn(question, expected_handoff=False, expected_chunk_id=chunk["id"]),
                }
            )
    return cases


def build_handoff_cases() -> list[dict]:
    return [{"category": "handoff", **_turn(msg, expected_handoff=True)} for msg in HANDOFF_MESSAGES]


def build_clinical_cases() -> list[dict]:
    return [
        {"category": "clinical", **_turn(msg, expected_handoff=True, expected_clinical_guard=True)}
        for msg in CLINICAL_MESSAGES
    ]


def build_out_of_domain_cases() -> list[dict]:
    return [{"category": "out_of_domain", **_turn(msg, expected_handoff=False)} for msg in OUT_OF_DOMAIN_MESSAGES]


def build_multi_turn_cases(chunks: list[dict], count: int) -> list[dict]:
    """
    2-4 turn conversations mixing categories: rag->rag, rag->handoff,
    rag->clinical, rag->rag->handoff. Deterministically cycles through the
    knowledge base and the handoff/clinical message pools rather than
    sampling randomly, so regeneration is reproducible.
    """
    patterns = ["rag_rag", "rag_handoff", "rag_clinical", "rag_rag_handoff"]
    cases = []
    for i in range(count):
        pattern = patterns[i % len(patterns)]
        chunk_a = chunks[(i * 3) % len(chunks)]
        chunk_b = chunks[(i * 3 + 7) % len(chunks)]

        turns = [_turn(_question_for_chunk(chunk_a, variant=i % 2), expected_handoff=False, expected_chunk_id=chunk_a["id"])]

        if pattern == "rag_rag":
            turns.append(_turn(_question_for_chunk(chunk_b, variant=(i + 1) % 2), expected_handoff=False, expected_chunk_id=chunk_b["id"]))
        elif pattern == "rag_handoff":
            turns.append(_turn(HANDOFF_MESSAGES[i % len(HANDOFF_MESSAGES)], expected_handoff=True))
        elif pattern == "rag_clinical":
            turns.append(_turn(CLINICAL_MESSAGES[i % len(CLINICAL_MESSAGES)], expected_handoff=True, expected_clinical_guard=True))
        elif pattern == "rag_rag_handoff":
            turns.append(_turn(_question_for_chunk(chunk_b, variant=(i + 1) % 2), expected_handoff=False, expected_chunk_id=chunk_b["id"]))
            turns.append(_turn(HANDOFF_MESSAGES[(i + 5) % len(HANDOFF_MESSAGES)], expected_handoff=True))

        cases.append({"category": "multi_turn", "pattern": pattern, "turns": turns})
    return cases


def generate(seed: int = 42, multi_turn_count: int = 28) -> list[dict]:
    chunks = [c.to_dict() for c in load_knowledge_base()]

    cases = (
        build_rag_grounded_cases(chunks)
        + build_handoff_cases()
        + build_clinical_cases()
        + build_out_of_domain_cases()
        + build_multi_turn_cases(chunks, multi_turn_count)
    )

    # Deterministic shuffle so categories aren't clustered in the file, but
    # regeneration is still reproducible byte-for-byte given the same inputs.
    random.Random(seed).shuffle(cases)

    for idx, case in enumerate(cases, start=1):
        case["id"] = idx
        case_with_id = {"id": idx, **{k: v for k, v in case.items() if k != "id"}}
        cases[idx - 1] = case_with_id

    return cases


if __name__ == "__main__":
    cases = generate()

    by_category: dict[str, int] = {}
    for case in cases:
        by_category[case["category"]] = by_category.get(case["category"], 0) + 1

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(cases, f, indent=2)

    print(f"Generated {len(cases)} cases -> {OUTPUT_PATH}")
    for category, count in sorted(by_category.items()):
        print(f"  {category}: {count}")
