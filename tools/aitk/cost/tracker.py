"""
Token-based AI cost estimates from provider-REPORTED usage (aitk.cost).

Three different things are kept apart on purpose:

  provider-reported usage   token counts a provider put in its response
                            (src/inference/llm_usage.py). The only usage accepted here.
  cost ESTIMATE             those counts x list prices YOU configured (pricing.json).
                            Every figure from this module is this -- an estimate.
  actual billing            what the provider invoices. Never computed here, and an
                            estimate can differ from it (see docs/AI_COST_TRACKING.md).

A cost is produced only when BOTH real usage and a matching configured price exist.
Otherwise the result says why, explicitly:

  CostStatus.PRICED          usage valid and a price configured for every token category used.
  CostStatus.UNPRICED        usage valid, but no price (or no price for a category with tokens).
  CostStatus.UNKNOWN_USAGE   usage missing, malformed, or not provider-reported.

Nothing is guessed or defaulted: no usage is invented, a missing price is never zero,
a missing cache price is never silently priced at the input rate, and aggregate totals
are withheld (``None``) whenever any recorded call is not PRICED.

The tracker reads only ``provider``, ``model`` and ``usage`` from a response dict. It
never touches message text, keeps no per-call record, and has no logger, so prompts,
responses and credentials cannot leak through it.

Usage::

    tracker = CostTracker(PricingTable.load("my_pricing.json"))
    estimate = tracker.record_response(final_dict_from_a_provider)
    results = tracker.to_results(Evidence.LIVE)          # aitk.results Results
"""

import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping, Optional

from aitk.core.schema import Evidence, Metric, Report, Result, Status
from aitk.cost.pricing import ModelPrice, PricingTable

USAGE_VERSION = 1
PROVIDER_REPORTED = "provider_reported"
BASIS = "token_estimate"

ESTIMATE_NOTE = (
    "Estimate: provider-reported token counts x configured list prices. Not an invoice; "
    "excludes failed or aborted attempts, taxes, discounts, free-tier allowances and minimum fees."
)

_MILLION = Decimal(1_000_000)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class CostStatus(StrEnum):
    PRICED = "PRICED"
    UNPRICED = "UNPRICED"
    UNKNOWN_USAGE = "UNKNOWN_USAGE"


class UsageError(ValueError):
    """A usage mapping is missing, malformed, or not provider-reported."""


def _safe_name(value: Any) -> str:
    """A bounded, control-character-free label for provider/model names taken from untrusted dicts."""
    if not isinstance(value, str):
        return ""
    return _CONTROL.sub("", value).strip()[:100]


def _count(data: Mapping[str, Any], key: str, *, required: bool) -> Optional[int]:
    if key not in data or data[key] is None:
        if required:
            raise UsageError(f"usage is missing required field {key!r}")
        return None
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UsageError(f"usage field {key!r} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class TokenUsage:
    """Validated canonical usage. ``input_tokens`` EXCLUDES cached tokens (see llm_usage.py)."""

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: Optional[int] = None
    cache_creation_input_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    response_model: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Any) -> "TokenUsage":
        if data is None:
            raise UsageError("no usage reported")
        if not isinstance(data, Mapping):
            raise UsageError("usage is not an object")
        if data.get("usage_version") != USAGE_VERSION:
            raise UsageError("unsupported or missing usage_version")
        if data.get("source") != PROVIDER_REPORTED:
            raise UsageError("usage is not provider-reported")
        output_tokens = _count(data, "output_tokens", required=True)
        reasoning = _count(data, "reasoning_tokens", required=False)
        if reasoning is not None and output_tokens is not None and reasoning > output_tokens:
            raise UsageError("reasoning_tokens exceeds output_tokens")
        model = _safe_name(data.get("response_model")) or None
        return cls(
            input_tokens=_count(data, "input_tokens", required=True) or 0,
            output_tokens=output_tokens or 0,
            cache_read_input_tokens=_count(data, "cache_read_input_tokens", required=False),
            cache_creation_input_tokens=_count(data, "cache_creation_input_tokens", required=False),
            reasoning_tokens=reasoning,
            response_model=model,
        )


@dataclass(frozen=True)
class CostEstimate:
    """The outcome of pricing ONE response. ``cost_usd`` is set only when ``status`` is PRICED."""

    status: CostStatus
    provider: str
    model: str
    usage: Optional[TokenUsage]
    cost_usd: Optional[Decimal]
    reason: str = ""
    price_source: Optional[str] = None
    price_effective_from: Optional[date] = None
    basis: str = field(default=BASIS, init=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "basis": self.basis,
            "provider": self.provider,
            "model": self.model,
            "cost_usd": None if self.cost_usd is None else str(self.cost_usd),
            "reason": self.reason,
            "price_source": self.price_source,
            "price_effective_from": None
            if self.price_effective_from is None
            else self.price_effective_from.isoformat(),
        }


def _compute(usage: TokenUsage, price: ModelPrice) -> tuple[Optional[Decimal], str]:
    """(cost, "") or (None, reason). A category with tokens but no configured price makes the call UNPRICED."""
    total = usage.input_tokens * price.input_per_million + usage.output_tokens * price.output_per_million
    if usage.cache_read_input_tokens:
        if price.cache_read_input_per_million is None:
            return None, "cache-read tokens present but no cache_read_input price is configured"
        total += usage.cache_read_input_tokens * price.cache_read_input_per_million
    if usage.cache_creation_input_tokens:
        if price.cache_creation_input_per_million is None:
            return None, "cache-creation tokens present but no cache_creation_input price is configured"
        total += usage.cache_creation_input_tokens * price.cache_creation_input_per_million
    return total / _MILLION, ""


def estimate_cost(
    pricing: PricingTable,
    *,
    provider: Any,
    model: Any,
    usage: Any,
    as_of: Optional[date] = None,
) -> CostEstimate:
    """
    Price one response. ``usage`` is the provider final dict's ``"usage"`` value (or None).
    The model used for the price lookup is the one the provider says it RAN
    (``usage["response_model"]``) when reported, else the configured ``model`` -- a
    configured alias such as "...-latest" is never assumed to still mean the model that was priced.
    """
    provider_name = _safe_name(provider)
    try:
        parsed = TokenUsage.from_mapping(usage)
    except UsageError as exc:
        return CostEstimate(CostStatus.UNKNOWN_USAGE, provider_name, _safe_name(model), None, None, str(exc))

    model_name = parsed.response_model or _safe_name(model)
    if not provider_name or not model_name:
        return CostEstimate(
            CostStatus.UNPRICED, provider_name, model_name, parsed, None, "provider or model identity is unknown"
        )
    price = pricing.find(provider_name, model_name, as_of)
    if price is None:
        when = f" on {as_of.isoformat()}" if as_of else ""
        return CostEstimate(
            CostStatus.UNPRICED,
            provider_name,
            model_name,
            parsed,
            None,
            f"no configured price for {provider_name}/{model_name}{when}",
        )
    cost, reason = _compute(parsed, price)
    if cost is None:
        return CostEstimate(
            CostStatus.UNPRICED,
            provider_name,
            model_name,
            parsed,
            None,
            reason,
            price.source,
            price.effective_from,
        )
    return CostEstimate(
        CostStatus.PRICED, provider_name, model_name, parsed, cost, "", price.source, price.effective_from
    )


@dataclass
class Totals:
    """Running totals for one (provider, model). Token sums cover every call with valid usage."""

    calls: int = 0
    priced_calls: int = 0
    unpriced_calls: int = 0
    unknown_usage_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    reasoning_tokens: int = 0
    priced_cost_usd: Decimal = Decimal(0)

    @property
    def complete(self) -> bool:
        """True only if at least one call was recorded and every one of them was PRICED."""
        return self.calls > 0 and self.priced_calls == self.calls

    @property
    def total_cost_usd(self) -> Optional[Decimal]:
        """The cost of all calls, or None (unknown) if any call is unpriced or has unknown usage."""
        return self.priced_cost_usd if self.complete else None

    def add(self, estimate: CostEstimate) -> None:
        self.calls += 1
        if estimate.usage is not None:
            self.input_tokens += estimate.usage.input_tokens
            self.output_tokens += estimate.usage.output_tokens
            self.cache_read_input_tokens += estimate.usage.cache_read_input_tokens or 0
            self.cache_creation_input_tokens += estimate.usage.cache_creation_input_tokens or 0
            self.reasoning_tokens += estimate.usage.reasoning_tokens or 0
        if estimate.status is CostStatus.PRICED and estimate.cost_usd is not None:
            self.priced_calls += 1
            self.priced_cost_usd += estimate.cost_usd
        elif estimate.status is CostStatus.UNPRICED:
            self.unpriced_calls += 1
        else:
            self.unknown_usage_calls += 1

    def copy(self) -> "Totals":
        return Totals(**self.__dict__)


@dataclass(frozen=True)
class CostSummary:
    by_model: dict[tuple[str, str], Totals]
    overall: Totals


class CostTracker:
    """
    Thread-safe aggregator of cost estimates. Memory is bounded by the number of distinct
    (provider, model) pairs, not by the number of calls.
    """

    def __init__(self, pricing: PricingTable) -> None:
        self._pricing = pricing
        self._lock = threading.Lock()
        self._by_model: dict[tuple[str, str], Totals] = {}
        self._overall = Totals()

    def record_usage(self, *, provider: Any, model: Any, usage: Any, as_of: Optional[date] = None) -> CostEstimate:
        estimate = estimate_cost(self._pricing, provider=provider, model=model, usage=usage, as_of=as_of)
        key = (estimate.provider or "unknown", estimate.model or "unknown")
        with self._lock:
            self._by_model.setdefault(key, Totals()).add(estimate)
            self._overall.add(estimate)
        return estimate

    def record_response(self, response: Any, *, as_of: Optional[date] = None) -> CostEstimate:
        """Record a provider's final response dict. Only provider, model and usage are read -- never the text."""
        if not isinstance(response, Mapping):
            return self.record_usage(provider=None, model=None, usage=None, as_of=as_of)
        return self.record_usage(
            provider=response.get("provider"), model=response.get("model"), usage=response.get("usage"), as_of=as_of
        )

    def summary(self) -> CostSummary:
        with self._lock:
            return CostSummary(
                by_model={key: totals.copy() for key, totals in sorted(self._by_model.items())},
                overall=self._overall.copy(),
            )

    def reset(self) -> None:
        with self._lock:
            self._by_model.clear()
            self._overall = Totals()

    # ── aitk.results integration ──────────────────────────────────────────────

    def to_results(self, evidence: Evidence) -> list[Result]:
        """
        One overall Result plus one per (provider, model). ``evidence`` is required: say what kind
        of usage this is (SIMULATED for synthetic responses, LIVE/PRODUCTION for real traffic).
        Status: PASS = every recorded call priced; PARTIAL = some calls unpriced or with unknown
        usage; NOT_RUN = nothing recorded. FAIL is never used (a usage report is not a pass/fail check).
        """
        summary = self.summary()
        results = [_result("ai_cost:overall", summary.overall, evidence)]
        for (provider, model), totals in summary.by_model.items():
            results.append(_result(f"ai_cost:{provider}:{model}", totals, evidence))
        return results

    def build_report(self, tool: str, evidence: Evidence, *, now: Optional[datetime] = None) -> Report:
        return Report.create(tool, self.to_results(evidence), now=now)


def _result(name: str, totals: Totals, evidence: Evidence) -> Result:
    metrics = {
        "calls": Metric(totals.calls),
        "priced_calls": Metric(totals.priced_calls),
        "unpriced_calls": Metric(totals.unpriced_calls),
        "unknown_usage_calls": Metric(totals.unknown_usage_calls),
        "input_tokens": Metric(totals.input_tokens, "tokens"),
        "output_tokens": Metric(totals.output_tokens, "tokens"),
        "cache_read_input_tokens": Metric(totals.cache_read_input_tokens, "tokens"),
        "cache_creation_input_tokens": Metric(totals.cache_creation_input_tokens, "tokens"),
        "reasoning_tokens": Metric(totals.reasoning_tokens, "tokens"),
        # The cost of the PRICED calls only. Always labelled as such; never presented as the total.
        "priced_cost_usd_estimate": Metric(float(totals.priced_cost_usd), "USD"),
    }
    total = totals.total_cost_usd
    if total is not None:
        metrics["total_cost_usd_estimate"] = Metric(float(total), "USD")
    if totals.calls == 0:
        status = Status.NOT_RUN
    elif totals.complete:
        status = Status.PASS
    else:
        status = Status.PARTIAL
    notes = ESTIMATE_NOTE
    if totals.calls and not totals.complete:
        notes += (
            f" INCOMPLETE: {totals.unpriced_calls} call(s) unpriced and {totals.unknown_usage_calls} with unknown "
            "usage, so no total cost is reported."
        )
    return Result(name=name, status=status, evidence=evidence, metrics=metrics, notes=notes)
