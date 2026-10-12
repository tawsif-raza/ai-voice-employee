"""
Offline tests for tools/aitk/cost (pricing.py, tracker.py).

All prices below are SYNTHETIC round numbers invented for these tests; none is a real
provider price. All usage is synthetic provider-shaped data -- no provider is called
(a fixture refuses real socket connections). Costs asserted here are exact Decimal
arithmetic on those fixtures, i.e. SIMULATED evidence.

See tests/aitk_support.py for when this module skips (production image) or fails
(AITK_REQUIRE_TOOLS=1).
"""

import copy
import json
import socket
import sys
import threading
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from aitk_support import ensure_toolkit_on_path

ensure_toolkit_on_path()

from aitk.core import report as report_io  # noqa: E402
from aitk.core.schema import Evidence, Status  # noqa: E402
from aitk.cost.pricing import DEFAULT_PRICING_PATH, ModelPrice, PricingError, PricingTable  # noqa: E402
from aitk.cost.tracker import (  # noqa: E402
    BASIS,
    ESTIMATE_NOTE,
    CostStatus,
    CostTracker,
    TokenUsage,
    UsageError,
    estimate_cost,
)

PROMPT_SENTINEL = "SENTINEL-PROMPT-TEXT-51be"
RESPONSE_SENTINEL = "SENTINEL-RESPONSE-TEXT-77aa"
D = Decimal


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    def _refuse(*args, **kwargs):
        raise AssertionError("a test attempted a real network connection")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)


PRICING = {
    "pricing_version": 1,
    "currency": "USD",
    "unit": "per_million_tokens",
    "models": [
        {
            "provider": "claude",
            "model": "test-model",
            "aliases": ["test-model-latest"],
            "effective_from": "2026-01-01",
            "source": "synthetic test fixture",
            "input_per_million": "1.00",
            "output_per_million": "2.00",
            "cache_read_input_per_million": "0.10",
            "cache_creation_input_per_million": "1.25",
        },
        {
            "provider": "gemini",
            "model": "gem-test",
            "effective_from": "2026-01-01",
            "source": "synthetic test fixture",
            "input_per_million": "0.50",
            "output_per_million": "1.50",
        },
    ],
}


def table(models=None):
    data = copy.deepcopy(PRICING)
    if models is not None:
        data["models"] = models
    return PricingTable.from_dict(data)


def entry(**overrides):
    base = copy.deepcopy(PRICING["models"][1])
    base.update(overrides)
    return base


def usage(**kw):
    base = {"usage_version": 1, "source": "provider_reported", "input_tokens": 100, "output_tokens": 50}
    base.update(kw)
    return base


def price_of(provider="claude", model="test-model", **kw):
    return estimate_cost(table(), provider=provider, model=model, usage=usage(**kw))


# ── Correct costs for known synthetic inputs ──────────────────────────────────


def test_known_cost_one_million_input_half_million_output():
    est = price_of(input_tokens=1_000_000, output_tokens=500_000)
    assert est.status is CostStatus.PRICED
    assert est.cost_usd == D("2.00")  # 1.00 x 1M/1M + 2.00 x 0.5M/1M


def test_known_cost_small_call_is_exact_decimal_arithmetic():
    assert price_of().cost_usd == D("0.0002")  # (100 x 1 + 50 x 2) / 1e6


def test_cached_token_categories_are_priced_separately():
    est = price_of(cache_read_input_tokens=1000, cache_creation_input_tokens=200)
    # (100 x 1.00 + 50 x 2.00 + 1000 x 0.10 + 200 x 1.25) / 1e6
    assert est.status is CostStatus.PRICED and est.cost_usd == D("0.00055")


def test_cache_read_is_cheaper_than_the_same_tokens_as_fresh_input():
    fresh = price_of(input_tokens=1100, output_tokens=0)
    cached = price_of(input_tokens=100, output_tokens=0, cache_read_input_tokens=1000)
    assert cached.cost_usd < fresh.cost_usd


def test_reasoning_tokens_are_a_subset_of_output_and_not_charged_twice():
    with_reasoning = price_of(output_tokens=50, reasoning_tokens=30)
    assert with_reasoning.cost_usd == price_of(output_tokens=50).cost_usd


def test_zero_tokens_is_a_real_zero_cost_not_unknown():
    est = price_of(input_tokens=0, output_tokens=0)
    assert est.status is CostStatus.PRICED and est.cost_usd == D(0)


def test_a_zero_price_is_an_explicit_free_price_and_costs_zero():
    free = table([entry(input_per_million=0, output_per_million="0")])
    est = estimate_cost(free, provider="gemini", model="gem-test", usage=usage(input_tokens=10**9))
    assert est.status is CostStatus.PRICED and est.cost_usd == D(0)


def test_gemini_style_usage_with_cached_tokens_and_no_cache_price_is_unpriced_not_guessed():
    est = estimate_cost(table(), provider="gemini", model="gem-test", usage=usage(cache_read_input_tokens=40))
    assert est.status is CostStatus.UNPRICED and est.cost_usd is None
    assert "cache_read_input" in est.reason
    assert est.usage is not None and est.usage.cache_read_input_tokens == 40  # usage is still known


def test_cache_creation_tokens_without_a_cache_creation_price_are_unpriced():
    models = [entry(provider="claude", model="m2", cache_read_input_per_million="0.1")]
    est = estimate_cost(table(models), provider="claude", model="m2", usage=usage(cache_creation_input_tokens=5))
    assert est.status is CostStatus.UNPRICED and "cache_creation_input" in est.reason


def test_zero_cache_tokens_do_not_require_a_cache_price():
    est = estimate_cost(table(), provider="gemini", model="gem-test", usage=usage(cache_read_input_tokens=0))
    assert est.status is CostStatus.PRICED and est.cost_usd == D("0.000125")  # (100 x .5 + 50 x 1.5) / 1e6


# ── Unknown costs: missing usage / missing pricing ────────────────────────────


@pytest.mark.parametrize("missing", [None, {}, [], "usage", 5])
def test_missing_or_non_object_usage_is_unknown_usage(missing):
    est = estimate_cost(table(), provider="claude", model="test-model", usage=missing)
    assert est.status is CostStatus.UNKNOWN_USAGE and est.cost_usd is None and est.usage is None and est.reason


def test_unknown_model_is_unpriced_with_usage_retained():
    est = estimate_cost(table(), provider="claude", model="never-configured", usage=usage())
    assert est.status is CostStatus.UNPRICED and est.cost_usd is None
    assert "claude/never-configured" in est.reason
    assert est.usage is not None and est.usage.input_tokens == 100


def test_empty_shipped_pricing_prices_nothing():
    shipped = PricingTable.load()
    assert len(shipped) == 0 and DEFAULT_PRICING_PATH.name == "pricing.json"
    est = estimate_cost(shipped, provider="claude", model="test-model", usage=usage())
    assert est.status is CostStatus.UNPRICED and est.cost_usd is None


def test_unknown_provider_with_a_known_model_name_is_unpriced():
    assert price_of(provider="gemini", model="test-model").status is CostStatus.UNPRICED


@pytest.mark.parametrize("provider,model", [(None, "test-model"), ("claude", None), ("", "test-model"), (5, 6)])
def test_unknown_identity_is_unpriced(provider, model):
    est = estimate_cost(table(), provider=provider, model=model, usage=usage())
    assert est.status is CostStatus.UNPRICED and est.cost_usd is None


@pytest.mark.parametrize(
    "bad",
    [
        {"input_tokens": -1},
        {"input_tokens": True},
        {"output_tokens": 1.5},
        {"output_tokens": "50"},
        {"input_tokens": None},
        {"output_tokens": None},
        {"cache_read_input_tokens": -3},
        {"cache_creation_input_tokens": "x"},
        {"reasoning_tokens": 999},  # exceeds output_tokens
        {"usage_version": 2},
        {"usage_version": None},
        {"source": "estimated"},
        {"source": None},
    ],
)
def test_malformed_or_non_provider_reported_usage_is_unknown_usage(bad):
    est = price_of(**bad)
    assert est.status is CostStatus.UNKNOWN_USAGE and est.cost_usd is None and est.usage is None


def test_estimated_usage_is_never_accepted_as_if_provider_reported():
    est = price_of(source="estimated")
    assert est.status is CostStatus.UNKNOWN_USAGE and "not provider-reported" in est.reason


def test_token_usage_parser_rejects_what_the_estimator_rejects():
    with pytest.raises(UsageError):
        TokenUsage.from_mapping(usage(input_tokens=-1))
    with pytest.raises(UsageError):
        TokenUsage.from_mapping(None)
    assert TokenUsage.from_mapping(usage(response_model="  x  ")).response_model == "x"


# ── Model identity: response model beats configured alias ─────────────────────


def test_alias_is_priced_when_no_response_model_was_reported():
    assert price_of(model="test-model-latest").status is CostStatus.PRICED


def test_response_model_is_used_for_pricing_over_the_configured_alias():
    models = [
        entry(provider="claude", model="snap-2", input_per_million="9.00", output_per_million="9.00"),
        entry(provider="claude", model="snap-1", aliases=["family-latest"]),
    ]
    est = estimate_cost(table(models), provider="claude", model="family-latest", usage=usage(response_model="snap-2"))
    assert est.model == "snap-2" and est.cost_usd == D("0.00135")  # (100 x 9 + 50 x 9) / 1e6, not snap-1's price


def test_unpriced_response_model_is_not_silently_priced_via_the_configured_alias():
    # The alias "test-model-latest" is priced, but the provider says it ran a different, unconfigured model.
    est = estimate_cost(
        table(), provider="claude", model="test-model-latest", usage=usage(response_model="other-snapshot")
    )
    assert est.status is CostStatus.UNPRICED and "other-snapshot" in est.reason


def test_models_are_matched_exactly_not_by_prefix_or_case():
    assert price_of(model="test-mode").status is CostStatus.UNPRICED
    assert price_of(model="test-model-2").status is CostStatus.UNPRICED
    assert price_of(model="Test-Model").status is CostStatus.UNPRICED


# ── Pricing configuration: dates, validation, ambiguity ───────────────────────


def dated():
    return table(
        [
            entry(effective_from="2026-01-01", effective_to="2026-06-30", input_per_million="1.00"),
            entry(effective_from="2026-07-01", input_per_million="3.00"),
        ]
    )


def test_price_changes_are_applied_by_effective_date():
    old = estimate_cost(
        dated(), provider="gemini", model="gem-test", usage=usage(output_tokens=0), as_of=date(2026, 6, 30)
    )
    new = estimate_cost(
        dated(), provider="gemini", model="gem-test", usage=usage(output_tokens=0), as_of=date(2026, 7, 1)
    )
    assert old.cost_usd == D("0.0001") and new.cost_usd == D("0.0003")
    assert old.price_effective_from == date(2026, 1, 1) and new.price_effective_from == date(2026, 7, 1)


def test_no_price_before_it_took_effect():
    est = estimate_cost(dated(), provider="gemini", model="gem-test", usage=usage(), as_of=date(2025, 12, 31))
    assert est.status is CostStatus.UNPRICED and "2025-12-31" in est.reason


def test_without_a_date_only_the_open_ended_price_is_used():
    est = estimate_cost(dated(), provider="gemini", model="gem-test", usage=usage(output_tokens=0))
    assert est.cost_usd == D("0.0003")
    only_closed = table([entry(effective_to="2026-06-30")])
    assert only_closed.find("gemini", "gem-test") is None  # unknown "now" -> unpriced, not stale


def test_estimate_records_which_price_was_used():
    est = price_of()
    assert est.price_source == "synthetic test fixture" and est.price_effective_from == date(2026, 1, 1)
    assert est.basis == BASIS == "token_estimate"


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda d: d.update(extra=1), "unknown key"),
        (lambda d: d.pop("models"), "missing required"),
        (lambda d: d.update(pricing_version=2), "unsupported pricing_version"),
        (lambda d: d.update(pricing_version=True), "unsupported pricing_version"),
        (lambda d: d.update(currency="EUR"), "unsupported currency"),
        (lambda d: d.update(unit="per_token"), "unsupported unit"),
        (lambda d: d.update(models={}), "must be a list"),
        (lambda d: d["models"].append("nope"), "must be an object"),
        (lambda d: d["models"][0].update(extra=1), "unknown key"),
        (lambda d: d["models"][0].pop("source"), "missing required"),
        (lambda d: d["models"][0].update(source="  "), "source"),
        (lambda d: d["models"][0].update(input_per_million="-1"), ">= 0"),
        (lambda d: d["models"][0].update(input_per_million="NaN"), ">= 0"),
        (lambda d: d["models"][0].update(input_per_million="Infinity"), ">= 0"),
        (lambda d: d["models"][0].update(output_per_million="abc"), "not a valid number"),
        (lambda d: d["models"][0].update(output_per_million=True), "number"),
        (lambda d: d["models"][0].update(output_per_million=None), "number"),
        (lambda d: d["models"][0].update(cache_read_input_per_million="-0.1"), ">= 0"),
        (lambda d: d["models"][0].update(effective_from="01/02/2026"), "ISO date"),
        (lambda d: d["models"][0].update(effective_from=None), "ISO date"),
        (lambda d: d["models"][0].update(effective_to="2025-01-01"), "before effective_from"),
        (lambda d: d["models"][0].update(aliases="x"), "must be a list"),
        (lambda d: d["models"][0].update(aliases=["test-model"]), "unique"),
        (lambda d: d["models"][0].update(aliases=["a", "a"]), "unique"),
        (lambda d: d["models"][0].update(aliases=[""]), "non-empty"),
    ],
)
def test_invalid_pricing_config_is_rejected_with_a_clear_error(mutate, message):
    data = copy.deepcopy(PRICING)
    mutate(data)
    with pytest.raises(PricingError, match=message):
        PricingTable.from_dict(data)


def test_non_object_config_is_rejected():
    for bad in (None, [], "x", 3):
        with pytest.raises(PricingError, match="must be an object"):
            PricingTable.from_dict(bad)


def test_overlapping_ranges_for_the_same_model_are_ambiguous():
    with pytest.raises(PricingError, match="ambiguous"):
        table([entry(effective_from="2026-01-01"), entry(effective_from="2026-03-01")])  # both open-ended
    with pytest.raises(PricingError, match="ambiguous"):
        table([entry(effective_to="2026-06-30"), entry(effective_from="2026-06-30")])  # share a day


def test_alias_collisions_across_entries_are_ambiguous_but_other_providers_are_fine():
    with pytest.raises(PricingError, match="ambiguous"):
        table([entry(model="a", aliases=["shared"]), entry(model="b", aliases=["shared"])])
    table([entry(provider="gemini", model="same"), entry(provider="groq", model="same")])  # no error


def test_pricing_json_text_loading_is_strict(tmp_path):
    good = json.dumps(PRICING)
    assert len(PricingTable.loads(good)) == 2
    with pytest.raises(PricingError, match="not valid JSON"):
        PricingTable.loads("{")
    with pytest.raises(PricingError, match="NaN and Infinity"):
        PricingTable.loads(good.replace('"1.00"', "NaN", 1))
    with pytest.raises(PricingError, match="duplicate key"):
        PricingTable.loads('{"pricing_version": 1, "pricing_version": 1}')
    path = tmp_path / "p.json"
    path.write_text(good, encoding="utf-8")
    assert len(PricingTable.load(path)) == 2
    with pytest.raises(FileNotFoundError):
        PricingTable.load(tmp_path / "missing.json")


def test_prices_are_decimal_from_text_not_binary_floats():
    entry_price = table([entry(input_per_million=0.1, output_per_million="0.2")]).prices[0]
    assert entry_price.input_per_million == D("0.1") and entry_price.output_per_million == D("0.2")
    assert isinstance(entry_price, ModelPrice)


def test_shipped_example_pricing_file_is_valid_and_clearly_marked_synthetic():
    example = DEFAULT_PRICING_PATH.with_name("pricing.example.json")
    parsed = PricingTable.load(example)
    assert len(parsed) == 1 and "EXAMPLE ONLY" in parsed.prices[0].source


# ── Aggregation ───────────────────────────────────────────────────────────────


def response(provider="claude", model="test-model", **usage_kw):
    return {
        "text": RESPONSE_SENTINEL,
        "latency_ms": 12.0,
        "provider": provider,
        "model": model,
        "usage": usage(**usage_kw),
    }


def test_token_aggregation_and_cost_over_several_priced_calls():
    tracker = CostTracker(table())
    tracker.record_response(response())
    tracker.record_response(response(input_tokens=300, output_tokens=0, cache_read_input_tokens=1000))
    tracker.record_response(response(provider="gemini", model="gem-test", input_tokens=10, output_tokens=2))
    summary = tracker.summary()
    claude = summary.by_model[("claude", "test-model")]
    assert (claude.calls, claude.priced_calls) == (2, 2)
    assert (claude.input_tokens, claude.output_tokens, claude.cache_read_input_tokens) == (400, 50, 1000)
    assert claude.total_cost_usd == D("0.0002") + D("0.0004")  # 0.0002 + (300 x 1 + 1000 x 0.10) / 1e6
    assert summary.overall.calls == 3 and summary.overall.input_tokens == 410
    assert summary.overall.total_cost_usd == D("0.0006") + D("0.000008")  # + (10 x .5 + 2 x 1.5) / 1e6


def test_one_unpriced_call_withholds_the_total_but_keeps_the_known_part():
    tracker = CostTracker(table())
    tracker.record_response(response())
    tracker.record_response(response(model="who-knows"))
    overall = tracker.summary().overall
    assert (overall.calls, overall.priced_calls, overall.unpriced_calls) == (2, 1, 1)
    assert overall.priced_cost_usd == D("0.0002")
    assert overall.total_cost_usd is None and not overall.complete
    assert overall.input_tokens == 200  # tokens of the unpriced call are still counted


def test_a_call_without_usage_makes_the_total_unknown():
    tracker = CostTracker(table())
    tracker.record_response(response())
    tracker.record_response({"provider": "claude", "model": "test-model", "text": RESPONSE_SENTINEL})
    overall = tracker.summary().overall
    assert overall.unknown_usage_calls == 1 and overall.total_cost_usd is None
    assert overall.input_tokens == 100  # nothing was invented for the call with no usage


def test_empty_tracker_has_no_total():
    overall = CostTracker(table()).summary().overall
    assert overall.calls == 0 and overall.total_cost_usd is None and not overall.complete


@pytest.mark.parametrize("junk", [None, 5, "text", [], ["usage"]])
def test_non_mapping_responses_are_counted_as_unknown_usage(junk):
    tracker = CostTracker(table())
    assert tracker.record_response(junk).status is CostStatus.UNKNOWN_USAGE
    assert tracker.summary().overall.unknown_usage_calls == 1


def test_models_are_aggregated_separately_and_in_sorted_order():
    tracker = CostTracker(table())
    tracker.record_response(response(provider="gemini", model="gem-test"))
    tracker.record_response(response())
    assert list(tracker.summary().by_model) == [("claude", "test-model"), ("gemini", "gem-test")]


def test_response_model_is_the_aggregation_key_when_reported():
    tracker = CostTracker(table([entry(provider="claude", model="snap-1")]))
    tracker.record_response(response(model="family-latest", response_model="snap-1"))
    assert list(tracker.summary().by_model) == [("claude", "snap-1")]


def test_summary_is_a_snapshot_and_reset_clears_everything():
    tracker = CostTracker(table())
    tracker.record_response(response())
    snapshot = tracker.summary()
    tracker.record_response(response())
    assert snapshot.overall.calls == 1 and tracker.summary().overall.calls == 2
    snapshot.overall.calls = 99  # mutating a snapshot must not corrupt the tracker
    assert tracker.summary().overall.calls == 2
    tracker.reset()
    assert tracker.summary().overall.calls == 0 and tracker.summary().by_model == {}


def test_tracker_is_thread_safe_and_exact():
    tracker = CostTracker(table())

    def work():
        for _ in range(125):
            tracker.record_response(response())

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    overall = tracker.summary().overall
    assert overall.calls == 1000 and overall.total_cost_usd == D("0.2")
    assert overall.input_tokens == 100_000 and overall.output_tokens == 50_000


def test_weird_names_are_sanitized_and_bounded():
    tracker = CostTracker(table())
    est = tracker.record_response(response(provider="cla\x00ude\n", model="m" * 500))
    assert "\x00" not in est.provider and "\n" not in est.provider and len(est.model) <= 100


# ── Privacy ───────────────────────────────────────────────────────────────────


def test_no_prompt_response_or_extra_fields_ever_reach_estimates_summaries_or_reports():
    tracker = CostTracker(table())
    noisy = response()
    noisy["messages"] = [{"role": "user", "content": PROMPT_SENTINEL}]
    noisy["api_key"] = "sk-SENTINEL"
    estimate = tracker.record_response(noisy)
    report = tracker.build_report("t", Evidence.SIMULATED, now=datetime(2026, 10, 12, tzinfo=timezone.utc))
    blob = repr(estimate) + repr(tracker.summary()) + json.dumps(estimate.to_dict()) + report_io.dumps(report)
    for secret in (PROMPT_SENTINEL, RESPONSE_SENTINEL, "sk-SENTINEL"):
        assert secret not in blob


# ── aitk.results integration ──────────────────────────────────────────────────


NOW = datetime(2026, 10, 12, 9, 30, tzinfo=timezone.utc)


def by_name(results):
    return {r.name: r for r in results}


def test_fully_priced_usage_reports_pass_with_a_total_and_the_estimate_disclaimer():
    tracker = CostTracker(table())
    tracker.record_response(response())
    results = by_name(tracker.to_results(Evidence.SIMULATED))
    overall = results["ai_cost:overall"]
    assert overall.status is Status.PASS and overall.evidence is Evidence.SIMULATED
    assert overall.metrics["total_cost_usd_estimate"].value == pytest.approx(0.0002)
    assert overall.metrics["total_cost_usd_estimate"].unit == "USD"
    assert overall.metrics["input_tokens"].value == 100 and overall.metrics["calls"].value == 1
    assert "Not an invoice" in overall.notes and overall.notes.startswith(ESTIMATE_NOTE)
    assert "ai_cost:claude:test-model" in results


def test_partially_priced_usage_reports_partial_and_omits_the_total():
    tracker = CostTracker(table())
    tracker.record_response(response())
    tracker.record_response(response(model="who-knows"))
    overall = by_name(tracker.to_results(Evidence.SIMULATED))["ai_cost:overall"]
    assert overall.status is Status.PARTIAL
    assert "total_cost_usd_estimate" not in overall.metrics
    assert overall.metrics["priced_cost_usd_estimate"].value == pytest.approx(0.0002)
    assert overall.metrics["unpriced_calls"].value == 1 and "INCOMPLETE" in overall.notes


def test_nothing_recorded_reports_not_run():
    overall = by_name(CostTracker(table()).to_results(Evidence.LOCAL))["ai_cost:overall"]
    assert overall.status is Status.NOT_RUN and "total_cost_usd_estimate" not in overall.metrics


def test_evidence_label_is_required_and_must_be_an_evidence_member():
    tracker = CostTracker(table())
    with pytest.raises(TypeError):
        tracker.to_results()  # type: ignore[call-arg]
    with pytest.raises(Exception, match="evidence"):
        tracker.to_results("LIVE")  # type: ignore[arg-type]


def test_report_round_trips_as_strict_json_with_the_phase_1_schema():
    tracker = CostTracker(table())
    tracker.record_response(response())
    tracker.record_response(response(provider="gemini", model="gem-test", cache_read_input_tokens=40))
    report = tracker.build_report("ai_cost_demo", Evidence.SIMULATED, now=NOW)
    text = report_io.dumps(report)
    data = json.loads(text)
    assert data["schema"] == "aitk.results" and data["schema_version"] == "1.0" and data["tool"] == "ai_cost_demo"
    assert {r["evidence"] for r in data["results"]} == {"SIMULATED"}
    assert report_io.loads(text) == report
    assert "NaN" not in text and "Infinity" not in text


def test_estimate_to_dict_is_json_safe_and_labels_itself_an_estimate():
    data = price_of().to_dict()
    assert json.loads(json.dumps(data)) == data
    assert data["basis"] == "token_estimate" and data["status"] == "PRICED" and data["cost_usd"] == "0.0002"
    unpriced = estimate_cost(table(), provider="x", model="y", usage=usage()).to_dict()
    assert unpriced["cost_usd"] is None and unpriced["status"] == "UNPRICED"


# ── Contract with src/inference (the dict shape is the only coupling) ─────────


def _load_providers():
    inference = str(Path(__file__).resolve().parents[1] / "src" / "inference")
    if inference not in sys.path:
        sys.path.insert(0, inference)
    import llm_provider

    return llm_provider


class _Stream:
    status_code = 200

    def __init__(self, events):
        self._lines = ["data: " + json.dumps(e) for e in events]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)


def _final(provider, events):
    from unittest.mock import patch

    with patch("requests.post", return_value=_Stream(events)):
        return list(provider.generate_stream([{"role": "user", "content": PROMPT_SENTINEL}]))[-1]


def test_claude_provider_output_prices_end_to_end():
    lp = _load_providers()
    events = [
        {
            "type": "message_start",
            "message": {"model": "test-model", "usage": {"input_tokens": 100, "output_tokens": 1}},
        },
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
        {"type": "message_delta", "usage": {"output_tokens": 50}},
    ]
    final = _final(lp.ClaudeLLMProvider(api_key="test-key", model="test-model-latest"), events)
    est = CostTracker(table()).record_response(final)
    assert est.status is CostStatus.PRICED and est.cost_usd == D("0.0002")


def test_gemini_provider_output_with_cached_tokens_prices_end_to_end():
    lp = _load_providers()
    chunk = {
        "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
        "usageMetadata": {"promptTokenCount": 140, "cachedContentTokenCount": 40, "candidatesTokenCount": 50},
    }
    models = [entry(cache_read_input_per_million="0.05")]
    final = _final(lp.GeminiLLMProvider(api_key="test-key", model="gem-test"), [chunk])
    est = CostTracker(table(models)).record_response(final)
    assert final["usage"]["input_tokens"] == 100  # 140 prompt - 40 cached
    assert est.status is CostStatus.PRICED
    assert est.cost_usd == D("0.000127")  # (100 x .5 + 50 x 1.5 + 40 x .05) / 1e6


def test_provider_response_without_usage_is_unknown_not_zero():
    lp = _load_providers()
    chunk = {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}
    final = _final(lp.GeminiLLMProvider(api_key="test-key", model="gem-test"), [chunk])
    est = CostTracker(table()).record_response(final)
    assert "usage" not in final and est.status is CostStatus.UNKNOWN_USAGE and est.cost_usd is None


def test_every_canonical_field_the_providers_can_emit_is_accepted_by_the_tracker():
    parsed = TokenUsage.from_mapping(
        {
            "usage_version": 1,
            "source": "provider_reported",
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 4,
            "reasoning_tokens": 1,
            "response_model": "m",
        }
    )
    assert (parsed.cache_read_input_tokens, parsed.cache_creation_input_tokens, parsed.reasoning_tokens) == (3, 4, 1)
