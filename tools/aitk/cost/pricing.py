"""
Versioned pricing configuration for AI cost estimates (aitk.cost).

A price exists only if someone wrote it down with a source and an effective date.
Nothing here is built in, guessed, or inferred: the shipped ``pricing.json`` is
EMPTY, so out of the box every cost is reported as unpriced. See
docs/AI_COST_TRACKING.md for how to maintain it.

File format (``pricing_version`` 1) -- strict JSON, unknown keys are errors::

    {
      "pricing_version": 1,
      "currency": "USD",
      "unit": "per_million_tokens",
      "models": [
        {
          "provider": "claude",                  # the repo's provider_name: claude | gemini | groq
          "model": "exact-model-id",             # matched EXACTLY (no prefixes, no wildcards)
          "aliases": ["exact-alias-id"],         # optional; other exact ids this price applies to
          "effective_from": "2026-01-01",        # first day this price applies
          "effective_to": null,                  # optional last day (inclusive); null = still current
          "source": "https://... or 'invoice 2026-09'",   # where YOU got the number
          "input_per_million": "1.00",
          "output_per_million": "2.00",
          "cache_read_input_per_million": "0.10",          # optional
          "cache_creation_input_per_million": "1.25"       # optional
        }
      ]
    }

Prices are parsed to ``Decimal`` (from their text), never binary floats. A price of
0 is allowed and means "I am explicitly saying this is free" (for example a free
tier); it is never a default. Two entries for the same provider and model name
whose date ranges overlap are an error, so a lookup is never ambiguous.
"""

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Union

PRICING_SCHEMA_VERSION = 1
DEFAULT_PRICING_PATH = Path(__file__).resolve().parent / "pricing.json"

_TOP_KEYS = ("pricing_version", "currency", "unit", "models")
_MODEL_KEYS = (
    "provider",
    "model",
    "aliases",
    "effective_from",
    "effective_to",
    "source",
    "input_per_million",
    "output_per_million",
    "cache_read_input_per_million",
    "cache_creation_input_per_million",
)
_REQUIRED_MODEL_KEYS = ("provider", "model", "effective_from", "source", "input_per_million", "output_per_million")


class PricingError(ValueError):
    """The pricing configuration is invalid or ambiguous."""


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PricingError(f"{what} must be a non-empty string, got {value!r}")
    return value.strip()


def _price(value: Any, what: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise PricingError(f"{what} must be a number or numeric string, got {value!r}")
    try:
        price = Decimal(str(value).strip())
    except InvalidOperation:
        raise PricingError(f"{what} is not a valid number: {value!r}") from None
    if not price.is_finite() or price < 0:
        raise PricingError(f"{what} must be a finite number >= 0, got {value!r}")
    return price


def _day(value: Any, what: str) -> date:
    try:
        return date.fromisoformat(_text(value, what))
    except ValueError:
        raise PricingError(f"{what} must be an ISO date (YYYY-MM-DD), got {value!r}") from None


@dataclass(frozen=True)
class ModelPrice:
    """List prices in USD per million tokens for one provider model over one date range."""

    provider: str
    model: str
    aliases: tuple[str, ...]
    effective_from: date
    effective_to: Optional[date]
    source: str
    input_per_million: Decimal
    output_per_million: Decimal
    cache_read_input_per_million: Optional[Decimal] = None
    cache_creation_input_per_million: Optional[Decimal] = None

    @property
    def names(self) -> tuple[str, ...]:
        return (self.model, *self.aliases)

    def covers(self, as_of: Optional[date]) -> bool:
        """True if this price applies on ``as_of``; with no date, only open-ended (current) prices do."""
        if as_of is None:
            return self.effective_to is None
        return self.effective_from <= as_of and (self.effective_to is None or as_of <= self.effective_to)

    @classmethod
    def from_dict(cls, data: Any) -> "ModelPrice":
        if not isinstance(data, Mapping):
            raise PricingError(f"each model entry must be an object, got {type(data).__name__}")
        unknown = sorted(set(data) - set(_MODEL_KEYS))
        if unknown:
            raise PricingError(f"model entry has unknown key(s): {unknown}")
        missing = [key for key in _REQUIRED_MODEL_KEYS if key not in data]
        if missing:
            raise PricingError(f"model entry is missing required key(s): {missing}")
        model = _text(data["model"], "model")
        raw_aliases = data.get("aliases", [])
        if not isinstance(raw_aliases, list):
            raise PricingError(f"aliases for {model!r} must be a list")
        aliases = tuple(_text(alias, f"alias of {model!r}") for alias in raw_aliases)
        if len(set(aliases)) != len(aliases) or model in aliases:
            raise PricingError(f"aliases for {model!r} must be unique and different from the model id")
        effective_from = _day(data["effective_from"], f"effective_from of {model!r}")
        effective_to = None if data.get("effective_to") is None else _day(data["effective_to"], "effective_to")
        if effective_to is not None and effective_to < effective_from:
            raise PricingError(f"effective_to is before effective_from for {model!r}")

        def optional_price(key: str) -> Optional[Decimal]:
            return None if data.get(key) is None else _price(data[key], f"{key} of {model!r}")

        return cls(
            provider=_text(data["provider"], "provider"),
            model=model,
            aliases=aliases,
            effective_from=effective_from,
            effective_to=effective_to,
            source=_text(data["source"], f"source of {model!r}"),
            input_per_million=_price(data["input_per_million"], f"input_per_million of {model!r}"),
            output_per_million=_price(data["output_per_million"], f"output_per_million of {model!r}"),
            cache_read_input_per_million=optional_price("cache_read_input_per_million"),
            cache_creation_input_per_million=optional_price("cache_creation_input_per_million"),
        )


_FAR_FUTURE = date.max


class PricingTable:
    """An immutable, validated set of ModelPrice entries with unambiguous lookup."""

    def __init__(self, prices: Iterable[ModelPrice] = ()) -> None:
        self._prices = tuple(prices)
        for i, a in enumerate(self._prices):
            for b in self._prices[i + 1 :]:
                shared = set(a.names) & set(b.names)
                if a.provider != b.provider or not shared:
                    continue
                if a.effective_from <= (b.effective_to or _FAR_FUTURE) and b.effective_from <= (
                    a.effective_to or _FAR_FUTURE
                ):
                    raise PricingError(
                        f"ambiguous pricing: {a.provider}/{sorted(shared)[0]} has overlapping date ranges"
                    )

    def __len__(self) -> int:
        return len(self._prices)

    @property
    def prices(self) -> tuple[ModelPrice, ...]:
        return self._prices

    def find(self, provider: str, model: str, as_of: Optional[date] = None) -> Optional[ModelPrice]:
        """
        The price for exactly this provider and model id (or a configured alias) that applies
        on ``as_of`` (``None`` = the current open-ended price). ``None`` if there is none --
        the caller must treat that as "unpriced", never as free.
        """
        matches = [p for p in self._prices if p.provider == provider and model in p.names and p.covers(as_of)]
        # __init__ rejects overlapping date ranges for the same provider and name, so there is
        # never more than one match to choose between.
        return matches[0] if matches else None

    @classmethod
    def from_dict(cls, data: Any) -> "PricingTable":
        if not isinstance(data, Mapping):
            raise PricingError(f"pricing config must be an object, got {type(data).__name__}")
        unknown = sorted(set(data) - set(_TOP_KEYS))
        if unknown:
            raise PricingError(f"pricing config has unknown key(s): {unknown}")
        missing = [key for key in _TOP_KEYS if key not in data]
        if missing:
            raise PricingError(f"pricing config is missing required key(s): {missing}")
        version = data["pricing_version"]
        if isinstance(version, bool) or version != PRICING_SCHEMA_VERSION:
            raise PricingError(
                f"unsupported pricing_version {version!r}; this reader supports {PRICING_SCHEMA_VERSION}"
            )
        if data["currency"] != "USD":
            raise PricingError(f"unsupported currency {data['currency']!r}; only 'USD' is supported")
        if data["unit"] != "per_million_tokens":
            raise PricingError(f"unsupported unit {data['unit']!r}; only 'per_million_tokens' is supported")
        models = data["models"]
        if not isinstance(models, list):
            raise PricingError("models must be a list")
        return cls(ModelPrice.from_dict(entry) for entry in models)

    @classmethod
    def loads(cls, text: str) -> "PricingTable":
        def reject_constant(token: str) -> Any:
            raise PricingError(f"invalid JSON number {token!r}: NaN and Infinity are not allowed")

        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise PricingError(f"duplicate key {key!r} in pricing config")
                result[key] = value
            return result

        try:
            data = json.loads(text, parse_constant=reject_constant, object_pairs_hook=reject_duplicates)
        except json.JSONDecodeError as exc:
            raise PricingError(f"pricing config is not valid JSON: {exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def load(cls, path: Union[str, Path, None] = None) -> "PricingTable":
        """Load a pricing file (default: the shipped, empty ``pricing.json``)."""
        return cls.loads(Path(path if path is not None else DEFAULT_PRICING_PATH).read_text(encoding="utf-8"))
