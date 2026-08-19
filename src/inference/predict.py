"""
Phase 5 / Extraction: Inference entry point and CLI, now a thin
backward-compatible facade over ConversationManager
(src/agent/conversation_manager.py).

Orchestration (clinical check -> retrieval -> prompt assembly -> generation
-> handoff check) used to live inline in this file's
VoiceAssistantInference class, fused with model loading. Per
docs/IMPLEMENTATION_ROADMAP.md milestone M2 and docs/adr/ADR-001, that
sequence has been extracted into ConversationManager; model loading and
generation have been extracted into LLMService
(src/inference/llm_service.py). VoiceAssistantInference now only builds
those two collaborators (via conversation_manager.build_conversation_manager)
and forwards calls, so every existing caller (src/api/server.py,
src/eval/evaluate.py, this file's own CLI) keeps working unmodified.
"""

import argparse
import sys
from pathlib import Path
from typing import Callable, Optional

from handoff_detector import HandoffDetector, HandoffMatch

_AGENT_DIR = str(Path(__file__).resolve().parents[1] / "agent")
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)
from conversation_manager import ConversationManager, build_conversation_manager  # noqa: E402


# ── Inference wrapper (backward-compatible facade) ────────────────────────────

class VoiceAssistantInference:
    """
    Backward-compatible facade over ConversationManager. Preserves the
    exact public interface (constructor signature, generate_response_stream/
    generate_response/detect_handoff/detect_handoff_scored/run_interactive,
    and the .tokenizer/.model/.device attributes) that existed before the
    Conversation Manager extraction, so no caller needs to change.

    New code should prefer conversation_manager.build_conversation_manager()
    directly (see src/api/server.py) — this class exists for the callers
    that already depend on VoiceAssistantInference's shape.
    """

    SYSTEM_PROMPT = ConversationManager.SYSTEM_PROMPT

    # Kept for backward compatibility — anyone reading
    # VoiceAssistantInference.HANDOFF_PHRASES directly still gets the same
    # list. Detection itself lives in HandoffDetector (see detect_handoff
    # below), a layered normalize/regex/synonym/semantic matcher configured
    # from configs/handoff_phrases.yaml — this list is only the fast-path
    # "exact phrase" layer within it.
    HANDOFF_PHRASES = HandoffDetector.DEFAULT_EXACT_PHRASES

    def __init__(
        self,
        base_model_name: str = "Qwen/Qwen2.5-0.5B-Instruct",
        adapter_path: Optional[str] = None,
        merge_weights: bool = True,
        max_new_tokens: int = 200,
        temperature: float = 0.7,
        top_p: float = 0.9,
        repetition_penalty: float = 1.1,
        auto_resolve_adapter: bool = True,
        handoff_config_path: Optional[str] = None,
        rag_enabled: bool = True,
        clinical_config_path: Optional[str] = None,
    ):
        """
        Args: unchanged from the pre-extraction constructor — see
        conversation_manager.build_conversation_manager() for the current
        docstring of each parameter; this class simply forwards them.
        """
        self._conversation_manager = build_conversation_manager(
            base_model_name=base_model_name,
            adapter_path=adapter_path,
            merge_weights=merge_weights,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
            auto_resolve_adapter=auto_resolve_adapter,
            handoff_config_path=handoff_config_path,
            rag_enabled=rag_enabled,
            clinical_config_path=clinical_config_path,
        )

        llm = self._conversation_manager.llm_service
        self.system_prompt = self._conversation_manager.system_prompt
        self.max_new_tokens = llm.max_new_tokens
        self.temperature = llm.temperature
        self.top_p = llm.top_p
        self.repetition_penalty = llm.repetition_penalty
        self.device = llm.device
        self.tokenizer = llm.tokenizer
        self.model = llm.model
        self.adapter_path = llm.adapter_path

    # ── Generation ───────────────────────────────────────────────────────────

    def generate_response_stream(
        self,
        user_input: str,
        history: Optional[list[dict]] = None,
    ):
        """
        Generate a response to `user_input`, yielding text chunks as
        they're produced, followed by a final dict summary once generation
        is complete. Delegates entirely to
        ConversationManager.handle_turn() — see that method's docstring
        for the exact yielded contract.
        """
        yield from self._conversation_manager.handle_turn(user_input, history=history)

    def generate_response(
        self,
        user_input: str,
        history: Optional[list[dict]] = None,
        on_token: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """
        Blocking wrapper around generate_response_stream: drains the
        stream, optionally forwarding each chunk to on_token as it
        arrives, and returns the final summary dict.
        """
        result: Optional[dict] = None
        for item in self.generate_response_stream(user_input, history=history):
            if isinstance(item, str):
                if on_token is not None:
                    on_token(item)
            else:
                result = item
        return result

    # ── Handoff detection ────────────────────────────────────────────────────

    def detect_handoff(self, response_text: str) -> bool:
        """
        Check whether a generated response signals a handoff to a human
        agent. Delegates to the same HandoffDetector instance the
        Conversation Manager uses post-generation. Kept bool-returning for
        backward compatibility; use detect_handoff_scored() for confidence
        and the matched layer/evidence.
        """
        return self._conversation_manager.handoff_detector.detect(response_text)

    def detect_handoff_scored(self, response_text: str) -> HandoffMatch:
        """Same check as detect_handoff(), but returns confidence plus which layer/evidence matched."""
        return self._conversation_manager.handoff_detector.score(response_text)

    # ── Interactive REPL ─────────────────────────────────────────────────────

    def run_interactive(self) -> None:
        """
        Run a terminal chat loop: read a line, stream the reply, show a
        handoff badge when triggered, repeat until 'exit'/EOF/Ctrl+C.
        """
        print("\nVoice Assistant — interactive chat. Type 'exit' or Ctrl+C to quit.\n")
        history: list[dict] = []

        while True:
            try:
                user_input = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting.")
                break

            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                break

            print("Assistant: ", end="", flush=True)
            result = self.generate_response(
                user_input,
                history=history,
                on_token=lambda tok: print(tok, end="", flush=True),
            )
            print()

            if result["is_handoff"]:
                print("[HANDOFF TRIGGERED]")
            print(f"({result['latency_ms']:.0f} ms)\n")

            history.append({"role": "user", "content": user_input})
            history.append({"role": "assistant", "content": result["response"]})


# ── Entry point ────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chat with the voice assistant")
    parser.add_argument("--base_model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument(
        "--adapter_path",
        default=None,
        help="LoRA checkpoint directory. Auto-resolved from outputs/ or "
             "models/qwen-voice-assistant if omitted.",
    )
    parser.add_argument(
        "--no-merge",
        action="store_true",
        help="Keep the LoRA adapter attached instead of merging into the base model.",
    )
    parser.add_argument("--max_new_tokens", type=int, default=200)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--top_p", type=float, default=0.9)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    assistant = VoiceAssistantInference(
        base_model_name=args.base_model,
        adapter_path=args.adapter_path,
        merge_weights=not args.no_merge,
        max_new_tokens=args.max_new_tokens,
        temperature=args.temperature,
        top_p=args.top_p,
    )
    assistant.run_interactive()
