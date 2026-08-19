"""
Loader for configs/reliability.yaml (Phase 10; plan.md Step 10.2).

Fail-safe, not fail-closed: unlike oidc_provider.py's load_oidc_config()
(a security boundary that must refuse to run under missing config),
reliability tuning is safe to default -- a missing or malformed
reliability.yaml falls back to conservative built-in values, mirroring
PolicyEngine's own `_load_yaml` resilience pattern (policy_engine.py).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "reliability.yaml"

_BUILTIN_DEFAULTS = {
    "llm": {"timeout_seconds": 60, "max_retries": 1, "base_delay_seconds": 0.5, "max_delay_seconds": 4.0},
    "rag": {"timeout_seconds": 5, "max_retries": 2, "base_delay_seconds": 0.2, "max_delay_seconds": 2.0},
    "tools": {"max_retries": 1, "base_delay_seconds": 0.2, "max_delay_seconds": 2.0},
    "circuit_breaker": {
        "llm": {"failure_threshold": 5, "recovery_timeout_seconds": 30},
        "rag": {"failure_threshold": 5, "recovery_timeout_seconds": 30},
        "tools": {"failure_threshold": 5, "recovery_timeout_seconds": 30},
    },
    "request_limits": {"max_message_length": 4000, "max_history_turns": 50, "max_history_turn_length": 4000},
    "max_concurrent_generations": 1,
    "graceful_shutdown_timeout_seconds": 30,
}


@dataclass(frozen=True)
class DependencyReliabilityConfig:
    timeout_seconds: float
    max_retries: int
    base_delay_seconds: float
    max_delay_seconds: float
    circuit_failure_threshold: int
    circuit_recovery_timeout_seconds: float


@dataclass(frozen=True)
class RequestLimits:
    max_message_length: int
    max_history_turns: int
    max_history_turn_length: int


@dataclass(frozen=True)
class ReliabilityConfig:
    llm: DependencyReliabilityConfig
    rag: DependencyReliabilityConfig
    tools: DependencyReliabilityConfig
    request_limits: RequestLimits
    max_concurrent_generations: int
    graceful_shutdown_timeout_seconds: float


def _merged(loaded: dict) -> dict:
    """Shallow-merges `loaded`'s `reliability:` section over the builtin defaults, key by key, tolerating a partially-specified file."""
    result = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _BUILTIN_DEFAULTS.items()}
    section = (loaded or {}).get("reliability", {}) or {}
    for key, value in section.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            merged_sub = dict(result[key])
            for sub_key, sub_value in value.items():
                if isinstance(sub_value, dict) and isinstance(merged_sub.get(sub_key), dict):
                    merged_sub[sub_key] = {**merged_sub[sub_key], **sub_value}
                else:
                    merged_sub[sub_key] = sub_value
            result[key] = merged_sub
        else:
            result[key] = value
    return result


def load_reliability_config(config_path: Optional[str] = None) -> ReliabilityConfig:
    path = Path(config_path) if config_path else _CONFIG_PATH
    loaded = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            loaded = {}
    data = _merged(loaded)
    cb = data["circuit_breaker"]

    def _dep(name: str) -> DependencyReliabilityConfig:
        d = data[name]
        cb_d = cb.get(name, _BUILTIN_DEFAULTS["circuit_breaker"][name])
        return DependencyReliabilityConfig(
            timeout_seconds=float(d.get("timeout_seconds", 30)),
            max_retries=int(d.get("max_retries", 0)),
            base_delay_seconds=float(d.get("base_delay_seconds", 0.2)),
            max_delay_seconds=float(d.get("max_delay_seconds", 2.0)),
            circuit_failure_threshold=int(cb_d.get("failure_threshold", 5)),
            circuit_recovery_timeout_seconds=float(cb_d.get("recovery_timeout_seconds", 30)),
        )

    limits = data["request_limits"]
    return ReliabilityConfig(
        llm=_dep("llm"), rag=_dep("rag"), tools=_dep("tools"),
        request_limits=RequestLimits(
            max_message_length=int(limits.get("max_message_length", 4000)),
            max_history_turns=int(limits.get("max_history_turns", 50)),
            max_history_turn_length=int(limits.get("max_history_turn_length", 4000)),
        ),
        max_concurrent_generations=int(data.get("max_concurrent_generations", 1)),
        graceful_shutdown_timeout_seconds=float(data.get("graceful_shutdown_timeout_seconds", 30)),
    )
