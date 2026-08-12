"""Jurisdiction-summed property tax rates and the Texas homestead warning."""

import json
from pathlib import Path

import pytest

from app.tax_rates import (
    county_config,
    effective_rate,
    homestead_rules,
    homestead_warning,
    table,
)

DATA = Path(__file__).resolve().parent.parent / "data"


class TestJurisdictionTable:
    def test_table_loads_and_is_memoised(self):
        assert table() is table()
        assert table()["counties"]

    def test_lubbock_is_configured_with_provenance(self):
        cfg = county_config("48303")
        assert cfg["state"] == "TX"
        assert cfg["tax_year"] and cfg["source"] and cfg["source_url"]
        assert cfg["countywide"] and cfg["cities"] and cfg["school_districts"]

    def test_every_rate_is_plausible(self):
        """A per-$100 rate above 3 or below 0 is a data-entry error."""
        for fips, cfg in table()["counties"].items():
            groups = ([("countywide", u) for u in cfg.get("countywide", [])]
                      + [("city", u) for u in cfg.get("cities", {}).values()]
                      + [("school", u) for u in cfg.get("school_districts", {}).values()])
            for kind, unit in groups:
                assert unit.get("name"), f"{fips} {kind} unit has no name"
                assert 0 <= float(unit["rate"]) < 3, f"{fips} {unit['name']} rate {unit['rate']}"

    def test_unknown_county_has_no_config(self):
        assert county_config("99999") is None


class TestEffectiveRate:
    def test_sums_county_city_and_school(self, monkeypatch):
        monkeypatch.setattr("app.tax_rates._school_district", lambda cfg, lat, lon: "LUBBOCK")
        got = effective_rate("48303", 33.53, -101.85, "LUBBOCK")
        assert got["complete"] is True
        assert got["school_district"] == "LUBBOCK"
        assert got["city"] == "City of Lubbock"
        # county .327425 + hospital .09966 + water .00295 + city .472191 + LISD .8672
        assert got["effective_rate_pct"] == pytest.approx(1.7694, abs=0.0002)
        assert len(got["units"]) == 5

    def test_school_district_changes_the_answer(self, monkeypatch):
        """The reason this module exists: the school rate dominates the spread."""
        def rate_for(district):
            monkeypatch.setattr("app.tax_rates._school_district", lambda c, a, b: district)
            return effective_rate("48303", 33.5, -101.9, "LUBBOCK")["effective_rate_pct"]
        lubbock, frenship = rate_for("LUBBOCK"), rate_for("FRENSHIP")
        assert frenship > lubbock
        assert frenship - lubbock == pytest.approx(1.1567 - 0.8672, abs=0.0002)

    def test_incomplete_when_district_is_unknown(self, monkeypatch):
        monkeypatch.setattr("app.tax_rates._school_district", lambda cfg, lat, lon: None)
        got = effective_rate("48303", 33.53, -101.85, "LUBBOCK")
        assert got["complete"] is False
        assert any("school" in n.lower() for n in got["notes"])
        assert got["effective_rate_pct"] < 1.0, "no school rate means a visibly partial total"

    def test_unincorporated_address_gets_no_city_rate(self, monkeypatch):
        monkeypatch.setattr("app.tax_rates._school_district", lambda cfg, lat, lon: "ROOSEVELT")
        got = effective_rate("48303", 33.5, -101.7, "ACUFF")
        assert got["city"] is None
        assert any("not an incorporated city" in n for n in got["notes"])
        assert all(u["name"] != "City of Lubbock" for u in got["units"])

    def test_units_are_itemised_for_audit(self, monkeypatch):
        monkeypatch.setattr("app.tax_rates._school_district", lambda cfg, lat, lon: "LUBBOCK")
        got = effective_rate("48303", 33.53, -101.85, "LUBBOCK")
        assert sum(u["rate_pct"] for u in got["units"]) == pytest.approx(got["effective_rate_pct"], abs=0.0002)
        assert all(u["unit_id"] for u in got["units"]), "every unit cites its comptroller ID"

    def test_unconfigured_county_returns_none(self):
        assert effective_rate("39035", 41.4, -81.6, "CLEVELAND") is None

    def test_a_dead_district_layer_degrades(self, monkeypatch):
        from app import tax_rates
        from app.data_sources import DataUnavailable
        def boom(url, params):
            raise DataUnavailable("down")
        monkeypatch.setattr("app.parcels._query", boom)
        got = tax_rates.effective_rate("48303", 33.53, -101.85, "LUBBOCK")
        assert got["complete"] is False


class TestHomesteadWarning:
    def test_fires_on_a_homesteaded_parcel(self):
        w = homestead_warning("TX", "HS;OA")
        assert w["cap_pct"] == 10
        assert "General residence homestead" in w["exemptions"]
        assert "Over-65" in w["exemptions"]
        assert "seller" in w["headline"].lower()

    def test_silent_without_an_exemption(self):
        assert homestead_warning("TX", None) is None
        assert homestead_warning("TX", "") is None

    def test_silent_outside_texas(self):
        assert homestead_warning("OH", "HS") is None
        assert homestead_warning(None, "HS") is None

    def test_agricultural_valuation_is_not_a_homestead(self):
        assert homestead_warning("TX", "AG") is None

    def test_comma_separated_codes_parse(self):
        w = homestead_warning("TX", "HS, DV")
        assert w and set(w["codes"]) == {"HS", "DV"}

    def test_reports_the_gap_when_paying_above_appraisal(self):
        w = homestead_warning("TX", "HS", appraised_value=180000, purchase_price=225000)
        assert w["reappraisal_gap"] == 45000
        assert w["reappraisal_gap_pct"] == pytest.approx(25.0, abs=0.1)

    def test_no_gap_when_buying_at_or_below_appraisal(self):
        w = homestead_warning("TX", "HS", appraised_value=180000, purchase_price=175000)
        assert "reappraisal_gap" not in w

    def test_rules_describe_the_taxable_value_correctly(self):
        """The cap applies to taxable value, not the appraised market value."""
        rules = homestead_rules("TX")
        detail = rules["detail"].lower()
        assert "taxable value" in detail
        assert "not capped" in detail or "is not capped" in detail
        assert "purchase price" in detail
