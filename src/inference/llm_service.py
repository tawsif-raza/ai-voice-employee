"""
LLM Service — model loading and text generation only.

Extracted from VoiceAssistantInference (src/inference/predict.py) as part of
the Conversation Manager extraction (docs/IMPLEMENTATION_ROADMAP.md M2;
docs/adr/ADR-001). This is the "LLM Service" leaf module from
docs/MODULES.md section 7: it loads the base model + LoRA adapter and turns
an already-assembled prompt (a `messages` list) into generated text.

Per ARCHITECTURE.md Core Principle 2 and Communication Rule 5, this class
has no outbound access of any kind: no knowledge of safety rules, retrieval,
or business/tool logic. Everything it needs arrives as the `messages`
argument to generate_stream() — prompt/context assembly is the Conversation
Manager's job (src/agent/conversation_manager.py), not this module's.
"""

import queue
import threading
import time
from pathlib import Path
from typing import Optional

import torch
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    TextIteratorStreamer,
)
from peft import PeftModel


class LLMGenerationError(RuntimeError):
    """Raised when the underlying model.generate() call fails, or stalls without completing."""


class LLMService:
    """
    Loads the fine-tuned Qwen 2.5 voice assistant model and generates text
    from an already-assembled prompt. See module docstring for the
    text-in/text-out isolation this class is required to maintain.
    """

    # Where to look for a trained LoRA adapter when none is given explicitly,
    # in priority order. Matches train.py's default TrainingConfig.output_dir
    # (models/qwen-voice-assistant) plus the outputs/ layout used for
    # checkpointed runs (outputs/checkpoint-final, outputs/checkpoint-<N>).
    DEFAULT_SEARCH_ROOTS = [
        Path("outputs"),
        Path("models/qwen-voice-assistant"),
    ]

    # Safety net so a worker-thread generation failure can't hang the
    # streamer consumer forever waiting on a token that will never arrive
    # (the thread died without the generate() call reaching streamer.end()).
    # Generous enough not to interfere with normal CPU-bound generation.
    GENERATION_TIMEOUT_S = 300.0

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
            max_new_tokens:     Default generation cap per response.
            temperature:        Default sampling temperature (0 = greedy).
            top_p:               Default nucleus sampling threshold.
            repetition_penalty: Default penalty applied to repeated tokens.
            auto_resolve_adapter: When adapter_path is None, search
                                 DEFAULT_SEARCH_ROOTS for one. Set False when
                                 base_model_name already points at a merged,
                                 standalone model.
        """
        self.max_new_tokens = max_new_tokens
        self.temperature = temperature
        self.top_p = top_p
        self.repetition_penalty = repetition_penalty

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

    def generate_stream(
        self,
        messages: list[dict],
        max_new_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        repetition_penalty: Optional[float] = None,
    ):
        """
        Generate text from an already-assembled prompt.

        Args:
            messages: Full chat-template-ready message list (system prompt,
                      any context, history, and the current user turn) —
                      already assembled by the caller. This method does not
                      interpret or modify message content or roles.
            max_new_tokens / temperature / top_p / repetition_penalty:
                      Optional per-call overrides of the constructor defaults.

        Yields:
            str chunks as they're generated, then a final
            {"text": str, "latency_ms": float} dict. Callers can tell the
            two apart with isinstance(item, str).

        Raises:
            LLMGenerationError if the underlying model.generate() call
            fails, or stalls past GENERATION_TIMEOUT_S without completing.
        """
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
            self.tokenizer,
            skip_prompt=True,
            skip_special_tokens=True,
            timeout=self.GENERATION_TIMEOUT_S,
        )

        generation_kwargs = dict(
            input_ids=input_ids,
            attention_mask=attention_mask,
            streamer=streamer,
            max_new_tokens=max_new_tokens if max_new_tokens is not None else self.max_new_tokens,
            do_sample=(temperature if temperature is not None else self.temperature) > 0,
            temperature=max(temperature if temperature is not None else self.temperature, 1e-5),
            top_p=top_p if top_p is not None else self.top_p,
            repetition_penalty=(
                repetition_penalty if repetition_penalty is not None else self.repetition_penalty
            ),
            pad_token_id=self.tokenizer.pad_token_id,
        )

        errors: list[BaseException] = []

        def _run_generate() -> None:
            try:
                self.model.generate(**generation_kwargs)
            except BaseException as exc:  # noqa: BLE001 - captured, re-raised on the caller's thread
                errors.append(exc)

        start = time.perf_counter()

        # generate() blocks until done, so it runs on a worker thread while
        # this thread drains the streamer — that's what makes the output
        # arrive incrementally instead of all at once at the end.
        worker = threading.Thread(target=_run_generate)
        worker.start()

        chunks: list[str] = []
        try:
            for chunk in streamer:
                if not chunk:
                    continue
                chunks.append(chunk)
                yield chunk
        except queue.Empty as exc:
            worker.join(timeout=1.0)
            raise LLMGenerationError(
                f"Generation stalled and timed out after {self.GENERATION_TIMEOUT_S}s."
            ) from exc
        finally:
            worker.join(timeout=1.0)

        if errors:
            raise LLMGenerationError(str(errors[0])) from errors[0]

        latency_ms = (time.perf_counter() - start) * 1000
        yield {"text": "".join(chunks).strip(), "latency_ms": latency_ms}
