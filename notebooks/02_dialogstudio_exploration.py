"""
Dialogstudio Exploration
Goal: Understand the structure of MULTIWOZ2_2 before cleaning it
"""

from datasets import load_dataset


def preview_dataset(name: str, config: str = None, split: str = "train", n: int = 2) -> None:
    """
    Load and preview the first n examples from a dataset.

    Args:
        name: HuggingFace dataset name
        config: Optional subset/config name
        split: Which split to load
        n: Number of examples to print
    """
    print(f"\n{'=' * 60}")
    print(f"Dataset : {name}")
    print(f"Config  : {config}")
    print(f"{'=' * 60}")

    ds = load_dataset(name, config, split=split)

    print(f"Total examples : {len(ds)}")
    print(f"Columns        : {ds.column_names}")
    print(f"\n--- First {n} examples ---\n")

    for i in range(min(n, len(ds))):
        print(f"[Example {i + 1}]")
        for col in ds.column_names:
            value = ds[i][col]
            # Pretty print lists and dicts so we can see the structure
            if isinstance(value, (list, dict)):
                import json

                value = json.dumps(value, indent=4)
                # Truncate if massive
                if len(value) > 800:
                    value = value[:800] + "\n... (truncated)"
            elif isinstance(value, str) and len(value) > 400:
                value = value[:400] + "... (truncated)"
            print(f"  {col}:")
            print(f"    {value}")
        print()


if __name__ == "__main__":
    preview_dataset(name="HuggingFaceH4/ultrachat_200k", config=None, split="train_sft", n=2)
