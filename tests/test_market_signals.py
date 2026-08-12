"""Tests for the market fundamentals layer.

The scoring and shaping logic is exercised offline against synthetic series;
tests marked `live` hit the real public sources.
"""

import json
from pathlib import Path

import pytest

from app.market_signals import (
    STATE_ABBR,
    _band,
    _metric,
    _months_between,
    _value_n_months_back,
    fundamentals,
    uhaul_growth_index,
)

DATA = Path(__file__).resolve().parent.parent / "data"


def months(n, start=(2025, 7)):
    """n consecutive month-start dates."""
    y, m = start
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}-01")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


class TestHelpers:
    def test_months_between(self):
        assert _months_between("2025-01-01", "2026-01-01") == 12
        assert _months_between("2026-06-01", "2025-06-01") == -12
        assert _months_between("2026-01-01", "2026-01-01") == 0

    def test_value_a_year_back(self):
        d = months(24)
        v = list(range(24))
        assert _value_n_months_back(d, v, 12) == 11  # 12 months before index 23

    def test_returns_none_when_history_too_short(self):
        d = months(4)
        assert _value_n_months_back(d, [1, 2, 3, 4], 12) is None

    def test_metric_computes_change(self):
        d = months(13)
        v = [100.0] * 12 + [110.0]
        m = _metric(d, v)
        assert m["latest"] == 110.0
        assert m["change_pct"] == pytest.approx(10.0)
        assert m["change_abs"] == pytest.approx(10.0)
        assert m["as_of"] == d[-1]

    def test_metric_survives_a_zero_baseline(self):
        d = months(13)
        m = _metric(d, [0.0] * 12 + [5.0])
        assert m["change_pct"] is None   # division by zero must not raise
        assert m["latest"] == 5.0

    def test_band_clamps_and_orients(self):
        assert _band(2.0, poor=-1.0, good=2.0) == 1.0
        assert _band(-5.0, poor=-1.0, good=2.0) == 0.0
        assert _band(0.5, poor=-1.0, good=2.0) == pytest.approx(0.5)
        # Inverted: lower is better.
        assert _band(30.0, poor=90.0, good=30.0) == 1.0
        assert _band(90.0, poor=90.0, good=30.0) == 0.0
        assert _band(None, 0, 1) is None


def signals_with(**metrics):
    return {"metrics": metrics, "national_unemployment": {"latest": 4.1, "as_of": "2026-06-01"}}


class TestFundamentals:
    def test_no_data_is_reported_not_guessed(self):
        f = fundamentals({"metrics": {}})
        assert f["score"] is None
        assert f["label"] == "No data"

    def test_strong_market_outscores_weak_market(self):
        d = months(13)
        strong = fundamentals(
            signals_with(employment=_metric(d, [100.0] * 12 + [103.0]),
                         unemployment_rate=_metric(d, [3.0] * 13)),
            demographics={"growth_pct_per_year": 2.0, "growth_since": "2020",
                          "population": 500000, "net_domestic_migration_per_1k": 6.0,
                          "net_domestic_migration": 3000, "as_of": "2025"},
            zip_metrics={"days_on_market": 25, "active_listings_yoy_pct": -8,
                         "price_cut_share_pct": 12, "pending_ratio": 0.9})
        weak = fundamentals(
            signals_with(employment=_metric(d, [100.0] * 12 + [97.0]),
                         unemployment_rate=_metric(d, [8.0] * 13)),
            demographics={"growth_pct_per_year": -1.5, "growth_since": "2020",
                          "population": 500000, "net_domestic_migration_per_1k": -9.0,
                          "net_domestic_migration": -4000, "as_of": "2025"},
            zip_metrics={"days_on_market": 110, "active_listings_yoy_pct": 55,
                         "price_cut_share_pct": 52, "pending_ratio": 0.05})
        assert strong["score"] > 80
        assert weak["score"] < 20
        assert strong["label"] == "Strong"
        assert weak["label"] == "Weak"

    def test_score_is_bounded(self):
        d = months(13)
        for delta in (-50.0, 0.0, 50.0):
            f = fundamentals(signals_with(employment=_metric(d, [100.0] * 12 + [100 + delta])))
            assert 0 <= f["score"] <= 100

    def test_partial_data_still_scores(self):
        d = months(13)
        f = fundamentals(signals_with(employment=_metric(d, [100.0] * 12 + [102.0])))
        assert f["score"] is not None
        assert f["components_used"] == ["job growth"]

    def test_zip_metrics_win_over_county(self):
        d = months(13)
        county = signals_with(price_cut_share=_metric(d, [45.0] * 13),
                              days_on_market=_metric(d, [80.0] * 13))
        with_zip = fundamentals(county, zip_metrics={"price_cut_share_pct": 12.0,
                                                     "days_on_market": 25.0})
        without = fundamentals(county)
        assert with_zip["score"] > without["score"]
        cuts = next(f for f in with_zip["findings"] if f["signal"] == "Price cuts")
        assert "12%" in cuts["value"] and "this ZIP" in cuts["text"]

    def test_findings_lead_with_problems(self):
        d = months(13)
        f = fundamentals(
            signals_with(employment=_metric(d, [100.0] * 12 + [95.0])),
            zip_metrics={"days_on_market": 25, "price_cut_share_pct": 10})
        verdicts = [x["verdict"] for x in f["findings"]]
        assert verdicts[0] == "bad", "problems should sort to the top"
        assert verdicts == sorted(verdicts, key=lambda v: {"bad": 0, "neutral": 1, "good": 2}[v])

    def test_every_finding_is_presentable(self):
        d = months(13)
        f = fundamentals(
            signals_with(employment=_metric(d, [100.0] * 12 + [102.0]),
                         unemployment_rate=_metric(d, [4.0] * 13)),
            demographics={"growth_pct_per_year": 1.0, "growth_since": "2020",
                          "population": 100000, "net_domestic_migration_per_1k": 2.0,
                          "net_domestic_migration": 200, "as_of": "2025"},
            uhaul={"state": "TX", "rank": 1, "of": 50, "year": 2025},
            zip_metrics={"days_on_market": 30, "price_cut_share_pct": 20,
                         "active_listings_yoy_pct": 5, "pending_ratio": 0.6})
        assert f["findings"]
        for x in f["findings"]:
            assert x["signal"] and x["value"] and x["text"]
            assert x["verdict"] in {"good", "neutral", "bad"}
            assert x["text"].endswith(".")

    def test_census_migration_preferred_over_uhaul(self):
        """U-Haul is state-level; a measured county figure must win."""
        f = fundamentals(
            signals_with(),
            demographics={"growth_pct_per_year": 1.0, "growth_since": "2020",
                          "population": 100000, "net_domestic_migration_per_1k": -5.0,
                          "net_domestic_migration": -500, "as_of": "2025"},
            uhaul={"state": "TX", "rank": 1, "of": 50, "year": 2025})
        mig = next(x for x in f["findings"] if x["signal"] == "Migration")
        assert mig["verdict"] == "bad"          # county outflow, despite TX ranking #1
        assert "-5.0 per 1,000" in mig["value"]
        assert "U-Haul" in mig["text"]          # still shown as context

    def test_uhaul_alone_is_weighted_lower(self):
        only_uhaul = fundamentals(signals_with(), uhaul={"state": "TX", "rank": 1, "of": 50, "year": 2025})
        assert only_uhaul["components_used"] == ["migration"]
        mig = only_uhaul["findings"][0]
        assert "directional" in mig["text"]


class TestUhaulData:
    def test_bundled_file_is_complete_and_sane(self):
        table = json.loads((DATA / "migration.json").read_text())["uhaul_growth_index"]
        ranks = table["rankings"]
        assert len(ranks) == 50
        assert sorted(ranks.values()) == list(range(1, 51)), "ranks must be a permutation of 1..50"
        assert table["year"] and table["source"] and table["note"]

    def test_lookup_returns_rank_and_provenance(self):
        tx = uhaul_growth_index("TX")
        assert tx["rank"] == 1 and tx["of"] == 50
        assert tx["year"] and "no API" in tx["note"]

    def test_unknown_state_is_none(self):
        assert uhaul_growth_index("ZZ") is None
        assert uhaul_growth_index(None) is None

    def test_suspect_source_values_were_dropped(self):
        table = json.loads((DATA / "migration.json").read_text())["uhaul_growth_index"]
        for st, row in table["detail"].items():
            a = row.get("arrivals_pct")
            assert a is None or 40 <= a <= 60, f"{st} has an implausible arrivals share"

    def test_state_abbreviations_cover_the_country(self):
        assert len(STATE_ABBR) == 51          # 50 states + DC
        assert STATE_ABBR["Ohio"] == "OH"


@pytest.mark.live
class TestLiveSignals:
    def test_zip_resolves_to_county(self):
        from app.data_sources import zip_to_county
        assert zip_to_county("44105")[0] == "39035"
        assert zip_to_county("78704")[0] == "48453"

    def test_county_report_is_populated(self):
        from app.market_signals import market_report
        r = market_report("44105")
        assert r["county"] == "Cuyahoga County"
        assert r["state"] == "OH"
        assert r["fundamentals"]["score"] is not None
        assert r["demographics"]["population"] > 1_000_000

    def test_zip_listings_and_rent_benchmarks(self):
        from app.data_sources import hud_safmr, realtor_zip
        z = realtor_zip("44105")
        assert z["days_on_market"] > 0 and z["median_list_price"] > 0
        b = hud_safmr("44105")
        beds = b["by_bedroom"]
        assert len(beds) == 5
        # More bedrooms cost more.
        assert [beds[k] for k in sorted(beds)] == sorted(beds.values())
