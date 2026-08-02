"""
Phase 5: Inference Engine and Human Handoff Logic
Goal: Load the fine-tuned Qwen 2.5 voice assistant (base model + LoRA
      adapter), stream responses token-by-token for low-latency playback,
      and detect when the model itself decides to hand the call off to a
      human agent.
"""

import argparse
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import torch
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    TextIteratorStreamer,
)
from peft import PeftModel


# ── Inference wrapper ─────────────────────────────────────────────────────────

class VoiceAssistantInference:
    """
    Wraps the fine-tuned Qwen 2.5 voice assistant for interactive inference.
    """

    # Must match the system prompt in src/data/preprocess.py — this is what
    # the model was fine-tuned on. Drifting from it here changes behavior.
    SYSTEM_PROMPT = (
        "You are a helpful, professional customer support voice assistant. "
        "Keep your responses brief, clear, and conversational. "
        "Never use bullet points or numbered lists. "
        "Speak naturally as if on a phone call. "
        "If you cannot help, offer to connect the customer to a human agent."
    )

    # Substrings (checked case-insensitively) that indicate the model has
    # decided to hand the conversation off to a human. Kept as plain
    # substring matches rather than regex — voice-assistant responses are
    # short and this is easier to extend as new phrasings show up.
    HANDOFF_PHRASES = [
        "connect you to a human",
        "connect you with a human",
        "connect you to an agent",
        "connect you with an agent",
        "connect you to a representative",
        "connect you with a representative",
        "connect you to someone",
        "transfer you to a human",
        "transfer you to an agent",
        "transfer you to a representative",
        "transfer your call",
        "transfer this call",
        "speak with a human",
        "speak to a human",
        "speak with a representative",
        "speak to a representative",
        "speak with an agent",
        "speak to an agent",
        "talk to a human",
        "talk to a representative",
        "talk to an agent",
        "human agent",
        "live agent",
        "customer service representative",
        "escalate this",
        "escalate you",
        "escalate your",
        "get you a human",
        "get a human",
        "reach a representative",
        "reach a human agent",
    ]

    # Where to look for a trained LoRA adapter when none is given explicitly,
    # in priority order. Matches train.py's default TrainingConfig.output_dir
    # (models/qwen-voice-assistant) plus the outputs/ layout used for
    # checkpointed runs (outputs/checkpoint-final, outputs/checkpoint-<N>).
    DEFAULT_SEARCH_ROOTS = [
        Path("outputs"),
        Path("models/qwen-voice-assistant"),
    ]

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
    ):
        """
        Args:
            base_model_name:    HuggingFace id of the base model, or a local
                                 directory (e.g. an already-merged export from
                                 src/export/merge_and_convert.py).
            adapter_path:       Explicit path to a LoRA checkpoint directory.
                                 If None, auto-resolves via DEFAULT_SEARCH_ROOTS
                                 (outputs/checkpoint-final, latest
                                 outputs/checkpoint-*, then the configured
                                 training output dir). Falls back to the base
                                 model alone if nothing is found.
            merge_weights:      Merge LoRA weights into the base model for
                                 faster inference. Set False to keep the
                                 adapter detachable.
            max_new_tokens:     Generation cap per response.
            temperature:        Sampling temperature (0 = greedy decoding).
            top_p:               Nucleus sampling threshold.
            repetition_penalty: Penalty applied to repeated tokens.
            auto_resolve_adapter: When adapter_path is None, search
                                 DEFAULT_SEARCH_ROOTS for one. Set False when
                                 base_model_name already points at a merged,
                                 standalone model — otherwise a LoRA adapter
                                 left over in outputs/ would get attached on
                                 top of a model it's already baked into.
        """
        self.max_new_tokens = max_new_tokens
        self.temperature = temperature
        self.top_p = top_p
        self.repetition_penalty = repetition_penalty
        self.system_prompt = self.SYSTEM_PROMPT

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"Device            : {self.device}")

        if adapter_path or auto_resolve_adapter:
            resolved_adapter = self._resolve_adapter_path(adapter_path)
        else:
            resolved_adapter = None
        tokenizer_source = str(resolved_adapter) if resolved_adapter else base_model_name

        print(f"Loading tokenizer : {tokenizer_source}")
        self.tokenizer = AutoTokenizer.from_pretrained(
            tokenizer_source, trust_remote_code=True
        )
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        print(f"Loading model     : {base_model_name}")
        if self.device == "cuda":
            bnb_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=torch.bfloat16,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
            )
            model = AutoModelForCausalLM.from_pretrained(
                base_model_name,
                quantization_config=bnb_config,
                device_map="auto",
                trust_remote_code=True,
            )
        else:
            model = AutoModelForCausalLM.from_pretrained(
                base_model_name,
                device_map="cpu",
                torch_dtype=torch.float32,
                trust_remote_code=True,
            )

        if resolved_adapter:
            print(f"Attaching adapter : {resolved_adapter}")
            model = PeftModel.from_pretrained(model, str(resolved_adapter))
            if merge_weights:
                print("Merging LoRA weights into base model...")
                model = model.merge_and_unload()
        else:
            print("WARNING: no LoRA checkpoint found in "
                  f"{[str(r) for r in self.DEFAULT_SEARCH_ROOTS]} — "
                  "running the base model with no fine-tuning.")

        model.eval()
        self.model = model
        self.adapter_path = resolved_adapter

    # ── Checkpoint resolution ───────────────────────────────────────────────

    def _resolve_adapter_path(self, explicit_path: Optional[str]) -> Optional[Path]:
        """
        Find a LoRA adapter directory to load.

        Priority: explicit path > outputs/checkpoint-final > latest
        numbered checkpoint under outputs/ > the configured training
        output dir (itself or its latest checkpoint).
        """
        if explicit_path:
            path = Path(explicit_path)
            if not (path / "adapter_config.json").exists():
                raise FileNotFoundError(
                    f"No adapter_config.json found in {path} — not a LoRA checkpoint dir."
                )
            return path

        for root in self.DEFAULT_SEARCH_ROOTS:
            final = root / "checkpoint-final"
            if (final / "adapter_config.json").exists():
                return final

            if (root / "adapter_config.json").exists():
                return root

            latest = self._latest_numbered_checkpoint(root)
            if latest is not None:
                return latest

        return None

    @staticmethod
    def _latest_numbered_checkpoint(root: Path) -> Optional[Path]:
        if not root.is_dir():
            return None

        numbered = []
        for candidate in root.glob("checkpoint-*"):
            suffix = candidate.name.rsplit("-", 1)[-1]
            if suffix.isdigit() and (candidate / "adapter_config.json").exists():
                numbered.append((int(suffix), candidate))

        if not numbered:
            return None

        numbered.sort(key=lambda pair: pair[0])
        return numbered[-1][1]

    # ── Generation ───────────────────────────────────────────────────────────

    def generate_response_stream(
        self,
        user_input: str,
        history: Optional[list[dict]] = None,
    ):
        """
        Generate a response to `user_input`, yielding text chunks as
        they're produced via TextIteratorStreamer, followed by a final
        dict summary once generation is complete.

        Args:
            user_input: The latest user turn.
            history:    Prior turns as [{"role": ..., "content": ...}, ...],
                        not including the system prompt or this turn.

        Yields:
            str chunks as they're generated, then a final
            {"response": str, "is_handoff": bool, "latency_ms": float}
            dict as the last item. Callers can tell the two apart with
            isinstance(item, str).
        """
        history = history or []
        messages = (
            [{"role": "system", "content": self.system_prompt}]
            + history
            + [{"role": "user", "content": user_input}]
        )

        input_ids = self.tokenizer.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_tensors="pt",
        ).to(self.model.device)

        streamer = TextIteratorStreamer(
            self.tokenizer, skip_prompt=True, skip_special_tokens=True
        )

        generation_kwargs = dict(
            input_ids=input_ids,
            streamer=streamer,
            max_new_tokens=self.max_new_tokens,
            do_sample=self.temperature > 0,
            temperature=max(self.temperature, 1e-5),
            top_p=self.top_p,
            repetition_penalty=self.repetition_penalty,
            pad_token_id=self.tokenizer.pad_token_id,
        )

        start = time.perf_counter()

        # generate() blocks until done, so it runs on a worker thread while
        # this thread drains the streamer — that's what makes the output
        # arrive incrementally instead of all at once at the end.
        worker = threading.Thread(target=self.model.generate, kwargs=generation_kwargs)
        worker.start()

        chunks: list[str] = []
        for chunk in streamer:
            if not chunk:
                continue
            chunks.append(chunk)
            yield chunk
        worker.join()

        latency_ms = (time.perf_counter() - start) * 1000
        response_text = "".join(chunks).strip()

        yield {
            "response": response_text,
            "is_handoff": self.detect_handoff(response_text),
            "latency_ms": latency_ms,
        }

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

        Returns:
            {"response": str, "is_handoff": bool, "latency_ms": float}
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
        Check whether a generated response signals a handoff to a human agent.

        Args:
            response_text: The model's generated reply.

        Returns:
            True if any known handoff phrase appears in the response.
        """
        lowered = response_text.lower()
        return any(phrase in lowered for phrase in self.HANDOFF_PHRASES)

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
