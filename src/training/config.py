"""
Phase 8: Training Configuration
Goal: Define all hyperparameters for model loading, LoRA setup,
      and the training loop in one place.

Keeping configuration separate from code is a production best practice.
Changing a hyperparameter should never require editing training logic.
"""

from dataclasses import dataclass, field


@dataclass
class ModelConfig:
    """
    Configuration for model loading and quantization.
    """

    # The model we are fine-tuning
    # We start with 0.5B for fast iteration and testing
    # Switch to 7B when you have access to a GPU with 16GB+ VRAM
    model_name: str = "Qwen/Qwen2.5-0.5B-Instruct"

    # Load in 4-bit quantization to reduce memory footprint
    # This makes fine-tuning possible on consumer hardware
    load_in_4bit: bool = True

    # 4-bit quantization data type
    # bfloat16 is more numerically stable than float16 for training
    bnb_4bit_compute_dtype: str = "bfloat16"

    # Quantization type — nf4 is the standard for QLoRA
    bnb_4bit_quant_type: str = "nf4"

    # Double quantization — quantizes the quantization constants
    # Saves an extra ~0.4 bits per parameter with negligible quality loss
    bnb_4bit_use_double_quant: bool = True

    # Maximum sequence length — must match what we used in dataset.py
    max_seq_length: int = 512


@dataclass
class LoRAConfig:
    """
    Configuration for LoRA adapter setup.
    These values control the size and behavior of the trainable adapters.
    """

    # Rank of the LoRA matrices A and B
    # Higher rank = more parameters = more capacity = more memory
    # 16 is a good starting point for a 0.5B model
    r: int = 16

    # Scaling factor applied to LoRA output
    # Convention: set to 2x rank for stable training
    lora_alpha: int = 32

    # Dropout rate on LoRA layers
    # Small value to prevent overfitting on our 1,366 examples
    lora_dropout: float = 0.05

    # Which weight matrices to apply LoRA to
    # These are the attention projection layers in transformer blocks:
    #   q_proj = query projection
    #   k_proj = key projection
    #   v_proj = value projection
    #   o_proj = output projection
    # We also include gate/up/down proj from feed-forward for better coverage
    target_modules: list[str] = field(default_factory=lambda: [
        "q_proj",
        "k_proj",
        "v_proj",
        "o_proj",
        "gate_proj",
        "up_proj",
        "down_proj",
    ])

    # Whether to train bias parameters
    # "none" is standard — biases add little value at extra memory cost
    bias: str = "none"

    # Task type — CAUSAL_LM = next token prediction (standard for fine-tuning)
    task_type: str = "CAUSAL_LM"


@dataclass
class TrainingConfig:
    """
    Configuration for the training loop.
    These are the hyperparameters that control how learning happens.
    """

    # Output directory for checkpoints and final model
    output_dir: str = "models/qwen-voice-assistant"

    # Number of times to iterate over the full dataset
    # 3 epochs is a safe starting point for our dataset size
    num_train_epochs: int = 3

    # Examples processed per GPU per step
    # Keep small (2-4) when fine-tuning on CPU or low VRAM GPU
    per_device_train_batch_size: int = 2

    # Accumulate gradients over N steps before updating weights
    # Effective batch size = per_device_train_batch_size × gradient_accumulation_steps
    # Here: 2 × 4 = 8 effective batch size
    gradient_accumulation_steps: int = 4

    # Learning rate — how fast the model updates its weights
    # 2e-4 is the standard starting point for LoRA fine-tuning
    learning_rate: float = 2e-4

    # Learning rate scheduler — how the learning rate changes over training
    # cosine = starts at learning_rate, smoothly decays to near zero
    lr_scheduler_type: str = "cosine"

    # Warmup steps — learning rate ramps up gradually at the start
    # Prevents large unstable updates in the first few batches
    warmup_steps: int = 10

    # Log training metrics every N steps
    logging_steps: int = 10

    # Save a checkpoint every N steps
    save_steps: int = 50

    # Maximum gradient norm — clips gradients to prevent exploding updates
    max_grad_norm: float = 0.3

    # Use bfloat16 precision during training forward pass
    bf16: bool = True

    # Disable full 16-bit training (we use bf16 instead)
    fp16: bool = False

    # Pack multiple short examples into one sequence for efficiency
    # Reduces padding waste — important for our short voice responses
    packing: bool = True

    # Random seed for reproducibility
    seed: int = 42


@dataclass
class DataConfig:
    """
    Configuration for data loading during training.
    """
    train_data_path: str = "data/processed/train_final.json"
    model_name:      str = "Qwen/Qwen2.5-0.5B-Instruct"
    max_length:      int = 512


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

    print("\n" + "="*60)
    print("MODEL CONFIG")
    print("="*60)
    for k, v in model_cfg.__dict__.items():
        print(f"  {k:<35} {v}")

    print("\n" + "="*60)
    print("LORA CONFIG")
    print("="*60)
    for k, v in lora_cfg.__dict__.items():
        print(f"  {k:<35} {v}")

    print("\n" + "="*60)
    print("TRAINING CONFIG")
    print("="*60)
    for k, v in train_cfg.__dict__.items():
        print(f"  {k:<35} {v}")