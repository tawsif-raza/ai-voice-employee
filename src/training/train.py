"""
Phase 9: Fine-Tuning with QLoRA
Goal: Load Qwen 2.5 in 4-bit quantization, attach LoRA adapters,
      and run supervised fine-tuning on our customer support dataset.

This script uses:
    - BitsAndBytes for 4-bit quantization
    - PEFT for LoRA adapter management
    - TRL's SFTTrainer for the training loop
    - HuggingFace Transformers for model and tokenizer
"""

import argparse
import json

import torch
from config import get_all_configs
from datasets import Dataset
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
)
from trl import SFTConfig, SFTTrainer

# ── Device check ───────────────────────────────────────────────────────────────


def check_device() -> str:
    """
    Check what compute device is available and report memory if GPU.

    Returns:
        Device string: 'cuda' or 'cpu'
    """
    if torch.cuda.is_available():
        device = "cuda"
        gpu_name = torch.cuda.get_device_name(0)
        vram_total = torch.cuda.get_device_properties(0).total_memory / 1e9
        vram_free = torch.cuda.memory_reserved(0) / 1e9
        print(f"Device     : {device} — {gpu_name}")
        print(f"VRAM total : {vram_total:.1f} GB")
        print(f"VRAM free  : {vram_total - vram_free:.1f} GB")
    else:
        device = "cpu"
        print("Device     : cpu")
        print("Note       : Training on CPU will be very slow.")
        print("             Even 0.5B model may take hours per epoch.")
        print("             Consider Google Colab (free T4 GPU) for training.")
    return device


# ── Model loading ──────────────────────────────────────────────────────────────


def load_model_and_tokenizer(model_cfg, device: str):
    """
    Load Qwen 2.5 in 4-bit quantization with BitsAndBytes.
    Prepare it for LoRA fine-tuning.

    Args:
        model_cfg: ModelConfig instance from config.py
        device:    'cuda' or 'cpu'

    Returns:
        Tuple of (model, tokenizer)
    """
    print("\nLoading tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained(
        model_cfg.model_name,
        trust_remote_code=True,
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    print("Loading model in 4-bit quantization...")

    if device == "cuda":
        # 4-bit quantization config — only works on GPU
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=model_cfg.load_in_4bit,
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_quant_type=model_cfg.bnb_4bit_quant_type,
            bnb_4bit_use_double_quant=model_cfg.bnb_4bit_use_double_quant,
        )
        model = AutoModelForCausalLM.from_pretrained(
            model_cfg.model_name,
            quantization_config=bnb_config,
            device_map="auto",
            trust_remote_code=True,
        )
    else:
        # CPU fallback — no quantization, full precision
        # Use this for testing that the code runs, not for real training
        model = AutoModelForCausalLM.from_pretrained(
            model_cfg.model_name,
            device_map="cpu",
            trust_remote_code=True,
            torch_dtype=torch.float32,
        )

    # Prepare model for k-bit training
    # This handles gradient checkpointing and layer casting
    # required when training a quantized model
    if device == "cuda":
        model = prepare_model_for_kbit_training(model)

    print("Model loaded successfully")
    return model, tokenizer


# ── LoRA setup ─────────────────────────────────────────────────────────────────


def apply_lora(model, lora_cfg):
    """
    Attach LoRA adapters to the model's attention and feed-forward layers.
    After this, only LoRA parameters are trainable — the base model is frozen.

    Args:
        model:    Loaded base model
        lora_cfg: LoRAConfig instance from config.py

    Returns:
        PEFT model with LoRA adapters attached
    """
    print("\nApplying LoRA adapters...")

    peft_config = LoraConfig(
        r=lora_cfg.r,
        lora_alpha=lora_cfg.lora_alpha,
        lora_dropout=lora_cfg.lora_dropout,
        target_modules=lora_cfg.target_modules,
        bias=lora_cfg.bias,
        task_type=lora_cfg.task_type,
    )

    model = get_peft_model(model, peft_config)

    # Print trainable parameter count
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    trainable_pct = trainable / total * 100

    print(f"Total parameters     : {total:,}")
    print(f"Trainable parameters : {trainable:,} ({trainable_pct:.2f}%)")
    print(f"Frozen parameters    : {total - trainable:,}")

    return model


# ── Data loading ───────────────────────────────────────────────────────────────


def load_training_data(data_path: str) -> Dataset:
    """
    Load train_final.json and convert to HuggingFace Dataset.
    SFTTrainer expects a dataset with a 'text' column containing
    the fully formatted ChatML string, or a 'messages' column
    for automatic formatting.

    Args:
        data_path: Path to train_final.json

    Returns:
        HuggingFace Dataset with messages column
    """
    print(f"\nLoading training data from: {data_path}")

    with open(data_path, "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    print(f"Loaded {len(raw_data)} examples")
    return Dataset.from_list(raw_data)


# ── Training ───────────────────────────────────────────────────────────────────


def run_training(model, tokenizer, dataset, train_cfg, model_cfg, device: str, resume: bool = False) -> None:
    """
    Run the supervised fine-tuning loop using TRL's SFTTrainer.
    SFTTrainer handles:
        - Automatic ChatML formatting via tokenizer.apply_chat_template
        - Loss masking on non-assistant tokens
        - Gradient accumulation
        - Checkpoint saving
        - Logging

    Args:
        model:     PEFT model with LoRA adapters
        tokenizer: Loaded tokenizer
        dataset:   HuggingFace Dataset with messages column
        train_cfg: TrainingConfig instance
        model_cfg: ModelConfig instance
        device:    'cuda' or 'cpu', from check_device() — bf16/fp16 mixed
                   precision requires a GPU; transformers raises a
                   ValueError if bf16/fp16 is requested without also
                   setting use_cpu=True on CPU-only setups.
        resume:    Resume from the latest checkpoint under
                   train_cfg.output_dir (per HF Trainer's
                   resume_from_checkpoint=True lookup) instead of
                   training from scratch.
    """
    print("\nInitializing SFTTrainer...")

    use_cpu = device == "cpu"
    bf16 = train_cfg.bf16 and not use_cpu
    fp16 = train_cfg.fp16 and not use_cpu

    sft_config = SFTConfig(
        output_dir=train_cfg.output_dir,
        num_train_epochs=train_cfg.num_train_epochs,
        per_device_train_batch_size=train_cfg.per_device_train_batch_size,
        gradient_accumulation_steps=train_cfg.gradient_accumulation_steps,
        learning_rate=train_cfg.learning_rate,
        lr_scheduler_type=train_cfg.lr_scheduler_type,
        warmup_steps=train_cfg.warmup_steps,
        logging_steps=train_cfg.logging_steps,
        save_steps=train_cfg.save_steps,
        max_grad_norm=train_cfg.max_grad_norm,
        bf16=bf16,
        fp16=fp16,
        use_cpu=use_cpu,
        packing=train_cfg.packing,
        seed=train_cfg.seed,
        max_steps=train_cfg.max_steps,
        assistant_only_loss=train_cfg.assistant_only_loss,
        max_length=model_cfg.max_seq_length,
        dataset_text_field=None,
    )

    trainer = SFTTrainer(
        model=model,
        processing_class=tokenizer,
        train_dataset=dataset,
        args=sft_config,
    )

    print("Starting training...")
    print(f"Epochs             : {train_cfg.num_train_epochs}")
    print(f"Examples           : {len(dataset)}")
    print(f"Effective batch    : {train_cfg.per_device_train_batch_size * train_cfg.gradient_accumulation_steps}")
    print(f"Output dir         : {train_cfg.output_dir}")
    print(f"Resume             : {resume}\n")

    trainer.train(resume_from_checkpoint=True if resume else None)

    print("\nTraining complete.")
    print(f"Saving final model to: {train_cfg.output_dir}")
    trainer.save_model(train_cfg.output_dir)
    tokenizer.save_pretrained(train_cfg.output_dir)
    print("Model saved.")


# ── CLI arguments ──────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    """
    Parse CLI overrides for the default configs in config.py.
    Leaving a flag unset keeps the corresponding config default.
    """
    parser = argparse.ArgumentParser(description="Fine-tune Qwen 2.5 with QLoRA")
    parser.add_argument(
        "--max_steps",
        type=int,
        default=None,
        help="Cap training to N optimizer steps (overrides num_train_epochs). "
        "Useful for smoke-testing the pipeline, e.g. --max_steps 10.",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default=None,
        help="Override the checkpoint/model output directory from TrainingConfig.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from the latest checkpoint found under --output_dir "
        "(or the configured TrainingConfig.output_dir) instead of "
        "starting training from scratch.",
    )
    return parser.parse_args()


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("QWEN 2.5 VOICE ASSISTANT — FINE-TUNING")
    print("=" * 60 + "\n")

    args = parse_args()

    # Load all configs
    model_cfg, lora_cfg, train_cfg, data_cfg = get_all_configs()

    if args.max_steps is not None:
        train_cfg.max_steps = args.max_steps
    if args.output_dir is not None:
        train_cfg.output_dir = args.output_dir

    # Check device
    print("[1/5] Checking device...")
    device = check_device()

    # Load model and tokenizer
    print("\n[2/5] Loading model and tokenizer...")
    model, tokenizer = load_model_and_tokenizer(model_cfg, device)

    # Apply LoRA
    print("\n[3/5] Applying LoRA...")
    model = apply_lora(model, lora_cfg)

    # Load data
    print("\n[4/5] Loading training data...")
    dataset = load_training_data(data_cfg.train_data_path)

    # Train
    print("\n[5/5] Running training...")
    run_training(model, tokenizer, dataset, train_cfg, model_cfg, device, resume=args.resume)
