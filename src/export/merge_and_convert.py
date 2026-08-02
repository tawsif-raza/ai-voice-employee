"""
Phase 8: Model Merging and Export Pipeline
Goal: Fuse the trained LoRA adapter into the base Qwen 2.5 weights to
      produce a standalone HF model, and optionally convert that model
      to quantized GGUF for local inference (Ollama / llama.cpp).
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import torch
import yaml
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel


# ── Config ───────────────────────────────────────────────────────────────────
# Defaults for this script's CLI flags live in configs/config.yaml under
# `export:`, the same single source of truth src/training/config.py reads.
# Falls back to the hardcoded defaults below if the file/key is missing.

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "config.yaml"


def _load_yaml() -> dict:
    if not _CONFIG_PATH.exists():
        return {}
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_YAML = _load_yaml()


def _get(key: str, default: Any) -> Any:
    return _YAML.get("export", {}).get(key, default)


# ── Checkpoint resolution ───────────────────────────────────────────────────
# Same search order as src/inference/predict.py. Duplicated rather than
# imported — this codebase keeps each src/ subdir import-independent.

DEFAULT_SEARCH_ROOTS = [
    Path("outputs"),
    Path("models/qwen-voice-assistant"),
]


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


def resolve_adapter_path(explicit_path: Optional[str]) -> Path:
    """
    Find the LoRA adapter directory to merge.

    Priority: explicit path > outputs/checkpoint-final > latest numbered
    checkpoint under outputs/ > the configured training output dir
    (itself or its latest checkpoint).

    Raises FileNotFoundError if nothing is found — export requires an
    actual fine-tuned adapter, unlike inference which can fall back to
    the base model.
    """
    if explicit_path:
        path = Path(explicit_path)
        if not (path / "adapter_config.json").exists():
            raise FileNotFoundError(
                f"No adapter_config.json found in {path} — not a LoRA checkpoint dir."
            )
        return path

    for root in DEFAULT_SEARCH_ROOTS:
        final = root / "checkpoint-final"
        if (final / "adapter_config.json").exists():
            return final

        if (root / "adapter_config.json").exists():
            return root

        latest = _latest_numbered_checkpoint(root)
        if latest is not None:
            return latest

    raise FileNotFoundError(
        f"No LoRA checkpoint found in {[str(r) for r in DEFAULT_SEARCH_ROOTS]}. "
        "Train a model first (scripts/run_training.sh) before exporting."
    )


# ── Merge ────────────────────────────────────────────────────────────────────

def merge_lora(
    base_model_name: str,
    adapter_path: Path,
    output_dir: Path,
    dtype: torch.dtype,
) -> None:
    """
    Load the base model + LoRA adapter, merge them into a standalone model,
    and save the fused model + tokenizer + generation config to output_dir.
    """
    print(f"Base model     : {base_model_name}")
    print(f"Adapter        : {adapter_path}")
    print(f"Dtype          : {dtype}")
    print(f"Output dir     : {output_dir}")

    print("\nLoading tokenizer...")
    tokenizer_source = str(adapter_path) if (adapter_path / "tokenizer_config.json").exists() else base_model_name
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_source, trust_remote_code=True)

    print("Loading base model (fp16/bf16, unquantized, CPU)...")
    model = AutoModelForCausalLM.from_pretrained(
        base_model_name,
        dtype=dtype,
        device_map="cpu",
        trust_remote_code=True,
    )

    print("Attaching LoRA adapter...")
    model = PeftModel.from_pretrained(model, str(adapter_path))

    print("Merging adapter weights into base model...")
    model = model.merge_and_unload()
    model.eval()

    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nSaving merged model to {output_dir}...")
    model.save_pretrained(output_dir, safe_serialization=True)
    tokenizer.save_pretrained(output_dir)

    # save_pretrained() only writes generation_config.json if the model
    # carries a GenerationConfig — make sure it's actually attached so
    # eos/pad token behavior survives the merge.
    if model.generation_config is not None:
        model.generation_config.save_pretrained(output_dir)

    print("Merge complete.")


def verify_export(output_dir: Path, run_smoke_test: bool) -> None:
    """
    Confirm the expected artifacts exist, and optionally reload the merged
    model to run a tiny generation as a functional smoke test.
    """
    print("\n" + "=" * 60)
    print("VERIFICATION")
    print("=" * 60)

    required = ["config.json", "tokenizer_config.json"]
    weight_files = list(output_dir.glob("*.safetensors")) + list(output_dir.glob("pytorch_model*.bin"))

    ok = True
    for name in required:
        exists = (output_dir / name).exists()
        print(f"  [{'OK' if exists else 'MISSING'}] {name}")
        ok = ok and exists

    print(f"  [{'OK' if weight_files else 'MISSING'}] model weights ({len(weight_files)} file(s))")
    ok = ok and bool(weight_files)

    if not ok:
        print("\nWARNING: merged model directory looks incomplete.")

    if run_smoke_test and ok:
        print("\nRunning smoke-test generation on the merged model...")
        tokenizer = AutoTokenizer.from_pretrained(str(output_dir), trust_remote_code=True)
        model = AutoModelForCausalLM.from_pretrained(
            str(output_dir), dtype=torch.float32, device_map="cpu", trust_remote_code=True
        )
        model.eval()
        messages = [{"role": "user", "content": "Hello, are you working?"}]
        input_ids = tokenizer.apply_chat_template(
            messages, tokenize=True, add_generation_prompt=True, return_tensors="pt"
        )
        with torch.no_grad():
            out = model.generate(input_ids, max_new_tokens=10, do_sample=False)
        text = tokenizer.decode(out[0][input_ids.shape[1]:], skip_special_tokens=True)
        print(f"  Sample output: {text!r}")
        print("  Smoke test passed — merged model loads and generates.")


# ── GGUF conversion ──────────────────────────────────────────────────────────

def find_llama_cpp_tools(llama_cpp_dir: Optional[str]) -> tuple[Path, Path]:
    """
    Locate convert_hf_to_gguf.py and the llama-quantize binary.

    Checks, in order: --llama_cpp_dir, $LLAMA_CPP_DIR, a `llama.cpp/`
    checkout next to the repo root, and finally the system PATH.

    Raises FileNotFoundError with setup instructions if not found.
    """
    import os

    candidates = []
    if llama_cpp_dir:
        candidates.append(Path(llama_cpp_dir))
    env_dir = os.environ.get("LLAMA_CPP_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(Path(__file__).resolve().parents[2] / "llama.cpp")

    convert_script = None
    quantize_bin = None

    for base in candidates:
        script = base / "convert_hf_to_gguf.py"
        if script.exists():
            convert_script = script
            for bin_candidate in (
                base / "llama-quantize",
                base / "llama-quantize.exe",
                base / "build" / "bin" / "llama-quantize",
                base / "build" / "bin" / "llama-quantize.exe",
                base / "build" / "bin" / "Release" / "llama-quantize.exe",
            ):
                if bin_candidate.exists():
                    quantize_bin = bin_candidate
                    break
            break

    if convert_script is None:
        path_hit = shutil.which("convert_hf_to_gguf.py")
        if path_hit:
            convert_script = Path(path_hit)

    if quantize_bin is None:
        path_hit = shutil.which("llama-quantize") or shutil.which("llama-quantize.exe")
        if path_hit:
            quantize_bin = Path(path_hit)

    if convert_script is None or quantize_bin is None:
        raise FileNotFoundError(
            "llama.cpp tools not found.\n\n"
            "To enable --export-gguf, clone and build llama.cpp:\n"
            "  git clone https://github.com/ggml-org/llama.cpp\n"
            "  cd llama.cpp\n"
            "  pip install -r requirements.txt\n"
            "  cmake -B build && cmake --build build --config Release -j\n\n"
            "Then either set LLAMA_CPP_DIR=/path/to/llama.cpp, or pass "
            "--llama_cpp_dir /path/to/llama.cpp, or place the checkout at "
            f"{Path(__file__).resolve().parents[2] / 'llama.cpp'}."
        )

    return convert_script, quantize_bin


def export_gguf(
    merged_model_dir: Path,
    gguf_output_dir: Path,
    quant_types: list[str],
    llama_cpp_dir: Optional[str],
) -> None:
    """
    Convert the merged HF model to GGUF fp16, then quantize it to each
    requested quant type (e.g. Q4_K_M, Q8_0).
    """
    print("\n" + "=" * 60)
    print("GGUF EXPORT")
    print("=" * 60)

    convert_script, quantize_bin = find_llama_cpp_tools(llama_cpp_dir)
    print(f"convert script : {convert_script}")
    print(f"quantize binary: {quantize_bin}")

    gguf_output_dir.mkdir(parents=True, exist_ok=True)
    fp16_path = gguf_output_dir / "model-f16.gguf"

    print(f"\nConverting merged model -> GGUF fp16: {fp16_path}")
    subprocess.run(
        [
            sys.executable, str(convert_script),
            str(merged_model_dir),
            "--outfile", str(fp16_path),
            "--outtype", "f16",
        ],
        check=True,
    )

    for quant in quant_types:
        quant_path = gguf_output_dir / f"model-{quant}.gguf"
        print(f"\nQuantizing -> {quant}: {quant_path}")
        subprocess.run(
            [str(quantize_bin), str(fp16_path), str(quant_path), quant],
            check=True,
        )

    print("\nGGUF export complete.")


# ── Reporting ────────────────────────────────────────────────────────────────

def print_file_sizes(label: str, directory: Path) -> None:
    if not directory.exists():
        return
    print(f"\n{label} ({directory}):")
    files = sorted(directory.rglob("*"))
    total = 0
    for f in files:
        if f.is_file():
            size = f.stat().st_size
            total += size
            print(f"  {size / 1e6:8.1f} MB  {f.relative_to(directory)}")
    print(f"  {'-' * 30}")
    print(f"  {total / 1e6:8.1f} MB  TOTAL")


def print_usage_instructions(merged_dir: Path, gguf_dir: Path, quant_types: list[str], gguf_exported: bool) -> None:
    print("\n" + "=" * 60)
    print("HOW TO LOAD THE EXPORTED MODEL")
    print("=" * 60)
    print(f"\nMerged HF model : {merged_dir}")
    print("  Load with transformers:")
    print(f"    AutoModelForCausalLM.from_pretrained(\"{merged_dir}\")")

    if not gguf_exported:
        print("\nGGUF not exported (run with --export-gguf to produce it).")
        return

    example_quant = quant_types[0] if quant_types else "Q4_K_M"
    gguf_file = gguf_dir / f"model-{example_quant}.gguf"

    print(f"\nGGUF model(s)   : {gguf_dir}")
    print("\n  --- llama.cpp ---")
    print(f"    ./llama-cli -m {gguf_file} -p \"Hello\" -cnv")
    print(f"    ./llama-server -m {gguf_file} --port 8080")

    print("\n  --- Ollama ---")
    print("    1. Create a Modelfile next to the .gguf:")
    print(f"         echo 'FROM {gguf_file}' > Modelfile")
    print("    2. Register it with Ollama:")
    print("         ollama create voice-assistant -f Modelfile")
    print("    3. Run it:")
    print("         ollama run voice-assistant")


# ── Entry point ────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge LoRA adapter into base model and optionally export to GGUF")
    parser.add_argument("--base_model", default=_get("base_model_name", "Qwen/Qwen2.5-0.5B-Instruct"))
    parser.add_argument(
        "--adapter_path",
        default=None,
        help="LoRA checkpoint directory. Auto-resolved from outputs/checkpoint-final "
             "or the latest outputs/checkpoint-* if omitted.",
    )
    parser.add_argument("--output_dir", default=_get("output_dir", "outputs/merged_model"),
                         help="Where to save the fused HF model.")
    parser.add_argument("--dtype", choices=["bf16", "fp16"], default=_get("dtype", "bf16"))
    parser.add_argument("--skip-verify", action="store_true", help="Skip the post-merge smoke-test generation.")

    parser.add_argument("--export-gguf", action="store_true", help="Also convert the merged model to GGUF.")
    parser.add_argument("--gguf_output_dir", default=_get("gguf_output_dir", "outputs/gguf"))
    parser.add_argument(
        "--quant",
        nargs="+",
        default=_get("quant_types", ["Q4_K_M", "Q8_0"]),
        help="GGUF quantization types to produce (default: Q4_K_M Q8_0).",
    )
    parser.add_argument(
        "--llama_cpp_dir",
        default=None,
        help="Path to a built llama.cpp checkout. Falls back to $LLAMA_CPP_DIR "
             "or a llama.cpp/ dir next to the repo root.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    dtype = torch.bfloat16 if args.dtype == "bf16" else torch.float16

    print("=" * 60)
    print("QWEN 2.5 VOICE ASSISTANT — MERGE & EXPORT")
    print("=" * 60)

    adapter_path = resolve_adapter_path(args.adapter_path)
    output_dir = Path(args.output_dir)

    merge_lora(args.base_model, adapter_path, output_dir, dtype)
    verify_export(output_dir, run_smoke_test=not args.skip_verify)
    print_file_sizes("Merged model files", output_dir)

    gguf_dir = Path(args.gguf_output_dir)
    if args.export_gguf:
        export_gguf(output_dir, gguf_dir, args.quant, args.llama_cpp_dir)
        print_file_sizes("GGUF files", gguf_dir)

    print_usage_instructions(output_dir, gguf_dir, args.quant, gguf_exported=args.export_gguf)

    print("\nDone.")
