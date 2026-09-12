"""
Phase 8: Training Configuration
Goal: Define all hyperparameters for model loading, LoRA setup,
      and the training loop in one place.

Keeping configuration separate from code is a production best practice.
Changing a hyperparameter should never require editing training logic.

Phase 9: Defaults are centralized in configs/config.yaml (the single
source of truth for model/training/LoRA/data parameters). This module
reads that file at import time; if it's missing or a key is absent, the
hardcoded default below is used instead, so this file still works
standalone (e.g. in a stripped-down container image without configs/).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "config.yaml"


def _load_yaml() -> dict:
    if not _CONFIG_PATH.exists():
        return {}
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_YAML = _load_yaml()


def _get(section: str, key: str, default: Any) -> Any:
    return _YAML.get(section, {}).get(key, default)


@dataclass
class ModelConfig:
    """
    Configuration for model loading and quantization.
    """

    # The model we are fine-tuning
    # We start with 0.5B for fast iteration and testing
    # Switch to 7B when you have access to a GPU with 16GB+ VRAM
    model_name: str = field(default_factory=lambda: _get("model", "model_name", "Qwen/Qwen2.5-0.5B-Instruct"))

    # Load in 4-bit quantization to reduce memory footprint
    # This makes fine-tuning possible on consumer hardware
    load_in_4bit: bool = field(default_factory=lambda: _get("model", "load_in_4bit", True))

    # 4-bit quantization data type
    # bfloat16 is more numerically stable than float16 for training
    bnb_4bit_compute_dtype: str = field(default_factory=lambda: _get("model", "bnb_4bit_compute_dtype", "bfloat16"))

    # Quantization type — nf4 is the standard for QLoRA
    bnb_4bit_quant_type: str = field(default_factory=lambda: _get("model", "bnb_4bit_quant_type", "nf4"))

    # Double quantization — quantizes the quantization constants
    # Saves an extra ~0.4 bits per parameter with negligible quality loss
    bnb_4bit_use_double_quant: bool = field(default_factory=lambda: _get("model", "bnb_4bit_use_double_quant", True))

    # Maximum sequence length — must match what we used in dataset.py
    max_seq_length: int = field(default_factory=lambda: _get("model", "max_seq_length", 512))


@dataclass
class LoRAConfig:
    """
    Configuration for LoRA adapter setup.
    These values control the size and behavior of the trainable adapters.
    """

    # Rank of the LoRA matrices A and B
    # Higher rank = more parameters = more capacity = more memory
    # 16 is a good starting point for a 0.5B model
    r: int = field(default_factory=lambda: _get("lora", "r", 16))

    # Scaling factor applied to LoRA output
    # Convention: set to 2x rank for stable training
    lora_alpha: int = field(default_factory=lambda: _get("lora", "lora_alpha", 32))

    # Dropout rate on LoRA layers
    # Small value to prevent overfitting on our 1,366 examples
    lora_dropout: float = field(default_factory=lambda: _get("lora", "lora_dropout", 0.05))

    # Which weight matrices to apply LoRA to
    # These are the attention projection layers in transformer blocks:
    #   q_proj = query projection
    #   k_proj = key projection
    #   v_proj = value projection
    #   o_proj = output projection
    # We also include gate/up/down proj from feed-forward for better coverage
    target_modules: list[str] = field(
        default_factory=lambda: _get(
            "lora",
            "target_modules",
            [
                "q_proj",
                "k_proj",
                "v_proj",
                "o_proj",
                "gate_proj",
                "up_proj",
                "down_proj",
            ],
        )
    )

    # Whether to train bias parameters
    # "none" is standard — biases add little value at extra memory cost
    bias: str = field(default_factory=lambda: _get("lora", "bias", "none"))

    # Task type — CAUSAL_LM = next token prediction (standard for fine-tuning)
    task_type: str = field(default_factory=lambda: _get("lora", "task_type", "CAUSAL_LM"))


@dataclass
class TrainingConfig:
    """
    Configuration for the training loop.
    These are the hyperparameters that control how learning happens.
    """

    # Output directory for checkpoints and final model
    output_dir: str = field(default_factory=lambda: _get("training", "output_dir", "models/qwen-voice-assistant"))

    # Number of times to iterate over the full dataset
    # 3 epochs is a safe starting point for our dataset size
    num_train_epochs: int = field(default_factory=lambda: _get("training", "num_train_epochs", 3))

    # Examples processed per GPU per step
    # Keep small (2-4) when fine-tuning on CPU or low VRAM GPU
    per_device_train_batch_size: int = field(default_factory=lambda: _get("training", "per_device_train_batch_size", 2))

    # Accumulate gradients over N steps before updating weights
    # Effective batch size = per_device_train_batch_size × gradient_accumulation_steps
    # Here: 2 × 4 = 8 effective batch size
    gradient_accumulation_steps: int = field(default_factory=lambda: _get("training", "gradient_accumulation_steps", 4))

    # Learning rate — how fast the model updates its weights
    # 2e-4 is the standard starting point for LoRA fine-tuning
    learning_rate: float = field(default_factory=lambda: _get("training", "learning_rate", 2e-4))

    # Learning rate scheduler — how the learning rate changes over training
    # cosine = starts at learning_rate, smoothly decays to near zero
    lr_scheduler_type: str = field(default_factory=lambda: _get("training", "lr_scheduler_type", "cosine"))

    # Warmup steps — learning rate ramps up gradually at the start
    # Prevents large unstable updates in the first few batches
    warmup_steps: int = field(default_factory=lambda: _get("training", "warmup_steps", 10))

    # Log training metrics every N steps
    logging_steps: int = field(default_factory=lambda: _get("training", "logging_steps", 10))

    # Save a checkpoint every N steps
    save_steps: int = field(default_factory=lambda: _get("training", "save_steps", 50))

    # Maximum gradient norm — clips gradients to prevent exploding updates
    max_grad_norm: float = field(default_factory=lambda: _get("training", "max_grad_norm", 0.3))

    # Use bfloat16 precision during training forward pass
    bf16: bool = field(default_factory=lambda: _get("training", "bf16", True))

    # Disable full 16-bit training (we use bf16 instead)
    fp16: bool = field(default_factory=lambda: _get("training", "fp16", False))

    # Pack multiple short examples into one sequence for efficiency
    # Reduces padding waste — important for our short voice responses
    packing: bool = field(default_factory=lambda: _get("training", "packing", True))

    # Random seed for reproducibility
    seed: int = field(default_factory=lambda: _get("training", "seed", 42))

    # Cap training to N optimizer steps, overriding num_train_epochs.
    # -1 disables the cap (train the full num_train_epochs).
    # Set via --max_steps on the CLI for smoke tests / dry runs.
    max_steps: int = field(default_factory=lambda: _get("training", "max_steps", -1))

    # Compute loss only on assistant-turn tokens (system/user tokens masked
    # with -100), matching the loss-masking behavior in dataset.py.
    # Without this, TRL's SFTTrainer computes loss over the full sequence.
    assistant_only_loss: bool = field(default_factory=lambda: _get("training", "assistant_only_loss", True))


@dataclass
class DataConfig:
    """
    Configuration for data loading during training.
    """

    train_data_path: str = field(
        default_factory=lambda: _get("data", "train_data_path", "data/processed/train_final.json")
    )
    model_name: str = field(default_factory=lambda: _get("data", "model_name", "Qwen/Qwen2.5-0.5B-Instruct"))
    max_length: int = field(default_factory=lambda: _get("data", "max_length", 512))


# ── Convenience function ───────────────────────────────────────────────────────


def get_all_configs() -> tuple[ModelConfig, LoRAConfig, TrainingConfig, DataConfig]:
    """
    Return all configuration objects with default values.
    Import and call this function from train.py to get configs.

    Returns:
        Tuple of (ModelConfig, LoRAConfig, TrainingConfig, DataConfig)
    """
    return ModelConfig(), LoRAConfig(), TrainingConfig(), DataConfig()


# ── Print config summary ───────────────────────────────────────────────────────

if __name__ == "__main__":
    model_cfg, lora_cfg, train_cfg, data_cfg = get_all_configs()

    print("\n" + "=" * 60)
    print("MODEL CONFIG")
    print("=" * 60)
    for k, v in model_cfg.__dict__.items():
        print(f"  {k:<35} {v}")

    print("\n" + "=" * 60)
    print("LORA CONFIG")
    print("=" * 60)
    for k, v in lora_cfg.__dict__.items():
        print(f"  {k:<35} {v}")

    print("\n" + "=" * 60)
    print("TRAINING CONFIG")
    print("=" * 60)
    for k, v in train_cfg.__dict__.items():
        print(f"  {k:<35} {v}")
