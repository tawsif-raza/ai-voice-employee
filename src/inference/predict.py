"""
Phase 5: Inference Engine and Human Handoff Logic
Goal: Load the fine-tuned Qwen 2.5 voice assistant (base model + LoRA
      adapter), stream responses token-by-token for low-latency playback,
      and detect when the model itself decides to hand the call off to a
      human agent.
"""

import argparse
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import torch
import yaml
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    TextIteratorStreamer,
)
from peft import PeftModel

from handoff_detector import HandoffDetector, HandoffMatch

# ── RAG config ───────────────────────────────────────────────────────────────
# configs/config.yaml's `rag:` section is the single source of truth for
# retrieval tuning (same pre-stub pattern src/eval/evaluate.py uses for its
# `evaluation:` section). Falls back to defaults if the file/key is missing.

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "config.yaml"
_CLINICAL_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "clinical_triggers.yaml"


def _load_rag_config() -> dict:
    if not _CONFIG_PATH.exists():
        return {}
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("rag", {}) or {}


_RAG_CONFIG = _load_rag_config()


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

    # Kept for backward compatibility — anyone reading
    # VoiceAssistantInference.HANDOFF_PHRASES directly still gets the same
    # list. Detection itself has moved to HandoffDetector (see
    # detect_handoff below), a layered normalize/regex/synonym/semantic
    # matcher configured from configs/handoff_phrases.yaml — this list is
    # only the fast-path "exact phrase" layer within it now.
    HANDOFF_PHRASES = HandoffDetector.DEFAULT_EXACT_PHRASES

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
        handoff_config_path: Optional[str] = None,
        rag_enabled: bool = True,
        clinical_config_path: Optional[str] = None,
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
            handoff_config_path: Optional override for the YAML file
                                 HandoffDetector reads (default:
                                 configs/handoff_phrases.yaml).
            rag_enabled:         Retrieve context from data/knowledge/*.json
                                 (via FAISS) and inject it into generation,
                                 and route clinical questions to a human
                                 instead of the model. Tuning (top_k,
                                 score threshold, knowledge/index dirs,
                                 embedding model) comes from configs/
                                 config.yaml's `rag:` section. Set False to
                                 skip loading faiss/sentence-transformers
                                 entirely (e.g. for a lightweight smoke test).
            clinical_config_path: Optional override for the YAML file the
                                 clinical-question guard reads (default:
                                 configs/clinical_triggers.yaml). Reuses
                                 HandoffDetector itself — see that file's
                                 header comment for why.
        """
        self.max_new_tokens = max_new_tokens
        self.temperature = temperature
        self.top_p = top_p
        self.repetition_penalty = repetition_penalty
        self.system_prompt = self.SYSTEM_PROMPT
        self._handoff_detector = HandoffDetector(config_path=handoff_config_path)

        self._rag_enabled = rag_enabled and bool(_RAG_CONFIG.get("enabled", True))
        self._rag_top_k = int(_RAG_CONFIG.get("top_k", 3))
        self._rag_score_threshold = float(_RAG_CONFIG.get("score_threshold", 0.35))
        self._retriever = None
        self._clinical_guard = None
        if self._rag_enabled:
            # Deferred import: faiss/sentence-transformers are only paid
            # for when RAG is actually enabled.
            rag_dir = str(Path(__file__).resolve().parents[1] / "rag")
            if rag_dir not in sys.path:
                sys.path.insert(0, rag_dir)
            from retriever import Retriever

            self._retriever = Retriever(
                knowledge_dir=_RAG_CONFIG.get("knowledge_dir"),
                index_dir=_RAG_CONFIG.get("index_dir"),
                embedding_model=_RAG_CONFIG.get("embedding_model", "sentence-transformers/all-MiniLM-L6-v2"),
            )
            self._clinical_guard = HandoffDetector(
                config_path=clinical_config_path
                or _RAG_CONFIG.get("clinical_triggers_path")
                or str(_CLINICAL_CONFIG_PATH)
            )

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
            {"response": str, "is_handoff": bool, "handoff_confidence": float,
             "latency_ms": float, "retrieved_chunks": list[dict],
             "clinical_guard_triggered": bool}
            dict as the last item. Callers can tell the two apart with
            isinstance(item, str).
        """
        history = history or []

        # Clinical questions (dosage, interactions, side effects, diagnosis,
        # ...) never reach the model — the small fine-tuned model was never
        # trained to ground clinical answers, and RAG only ever surfaces
        # non-clinical product facts, so the safe behavior is to route these
        # to a human deterministically rather than let the model improvise.
        if self._clinical_guard is not None:
            clinical_match = self._clinical_guard.score(user_input)
            if clinical_match.is_handoff:
                safe_response = (
                    "That's a question our pharmacist needs to answer directly "
                    "for your safety — let me connect you with one now."
                )
                yield safe_response
                yield {
                    "response": safe_response,
                    "is_handoff": True,
                    "handoff_confidence": clinical_match.confidence,
                    "latency_ms": 0.0,
                    "retrieved_chunks": [],
                    "clinical_guard_triggered": True,
                }
                return

        # Retrieve reference context and inject it as an extra system
        # message. Training (src/data/preprocess.py) never showed the model
        # a special "context block" format, so this is plain prose in a
        # system turn — the most consistent thing to do without retraining,
        # but grounding quality is inherently limited by that.
        retrieved_chunks = []
        context_message = None
        if self._retriever is not None:
            retrieved_chunks = self._retriever.retrieve(user_input, top_k=self._rag_top_k)
            relevant = [c for c in retrieved_chunks if c.score >= self._rag_score_threshold]
            if relevant:
                context_lines = "\n".join(f"- {c.title}: {c.content}" for c in relevant)
                context_message = {
                    "role": "system",
                    "content": (
                        "Reference information that may help answer the "
                        "customer's question, if relevant. Use it naturally "
                        "without mentioning that you looked anything up:\n"
                        + context_lines
                    ),
                }

        messages = [{"role": "system", "content": self.system_prompt}]
        if context_message is not None:
            messages.append(context_message)
        messages += history + [{"role": "user", "content": user_input}]

        # apply_chat_template(..., return_tensors="pt") returns a BatchEncoding
        # (not a bare tensor) on current transformers versions, so pull the
        # tensors out explicitly rather than passing the encoding straight
        # through to generate().
        encoded = self.tokenizer.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_tensors="pt",
            return_dict=True,
        ).to(self.model.device)
        input_ids = encoded["input_ids"]
        attention_mask = encoded.get("attention_mask")

        streamer = TextIteratorStreamer(
            self.tokenizer, skip_prompt=True, skip_special_tokens=True
        )

        generation_kwargs = dict(
            input_ids=input_ids,
            attention_mask=attention_mask,
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
        handoff_match = self._handoff_detector.score(response_text)

        yield {
            "response": response_text,
            "is_handoff": handoff_match.is_handoff,
            "handoff_confidence": handoff_match.confidence,
            "latency_ms": latency_ms,
            "retrieved_chunks": [c.to_dict() for c in retrieved_chunks],
            "clinical_guard_triggered": False,
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
            {"response": str, "is_handoff": bool, "handoff_confidence": float, "latency_ms": float}
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
        Delegates to HandoffDetector (handoff_detector.py): normalize ->
        exact phrase -> regex -> synonym -> semantic layers, configured
        from configs/handoff_phrases.yaml. Kept bool-returning for backward
        compatibility; use detect_handoff_scored() for confidence and the
        matched layer/evidence.

        Args:
            response_text: The model's generated reply.

        Returns:
            True if any layer signals handoff intent at or above the
            configured confidence threshold.
        """
        return self._handoff_detector.detect(response_text)

    def detect_handoff_scored(self, response_text: str) -> HandoffMatch:
        """Same check as detect_handoff(), but returns confidence plus which layer/evidence matched."""
        return self._handoff_detector.score(response_text)

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
