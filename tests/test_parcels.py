"""County parcel adapters: geometry, address matching, comp statistics."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.data_sources import DataUnavailable
from app.main import app
from app.parcels import (
    _epoch_ms_to_date,
    _quantile,
    _shape,
    _street_key,
    _trim_outliers,
    is_non_disclosure,
    normalize_address,
    registry,
    source_for,
    to_web_mercator,
)

client = TestClient(app)
DATA = Path(__file__).resolve().parent.parent / "data"


class TestGeometry:
    def test_origin(self):
        x, y = to_web_mercator(0.0, 0.0)
        assert x == pytest.approx(0.0) and y == pytest.approx(0.0, abs=1e-6)

    def test_known_point(self):
        # Cleveland, matching the value ArcGIS itself returns for this lon/lat.
        x, y = to_web_mercator(-81.795555858129, 41.485155400136)
        assert x == pytest.approx(-9105439.63, abs=1.0)
        assert y == pytest.approx(5084167.51, abs=1.0)

    def test_longitude_is_linear(self):
        assert to_web_mercator(90.0, 0)[0] == pytest.approx(to_web_mercator(45.0, 0)[0] * 2)

    def test_northern_latitudes_grow(self):
        assert to_web_mercator(0, 60)[1] > to_web_mercator(0, 30)[1] > 0


class TestAddressMatching:
    def test_suffix_and_whitespace_normalise(self):
        assert normalize_address("1453  Marlowe Avenue") == "1453 MARLOWE AVE"
        assert normalize_address("12 Oak Street.") == "12 OAK ST"

    def test_punctuation_is_stripped(self):
        assert normalize_address("1453 Marlowe Ave., Lakewood, OH") == \
            normalize_address("1453 MARLOWE AVE  LAKEWOOD OH")

    def test_none_is_empty(self):
        assert normalize_address(None) == ""

    def test_the_two_databases_agree_on_the_key(self):
        """Geocoder and county spell the same address differently."""
        geocoder = "1453 MARLOWE AVE, LAKEWOOD, OH, 44107"
        county = "1453  MARLOWE AVE, LAKEWOOD, OH, 44107"
        assert _street_key(geocoder) == _street_key(county) == ("1453", "MARLOWE")

    def test_neighbours_do_not_collide(self):
        assert _street_key("1453 Marlowe Ave") != _street_key("1455 Marlowe Ave")
        assert _street_key("1453 Marlowe Ave") != _street_key("1453 Lincoln Ave")

    def test_unparseable_address_yields_empty_key(self):
        assert _street_key("PO Box 12") == ("", "")
        assert _street_key(None) == ("", "")


class TestDates:
    def test_epoch_ms_converts(self):
        assert _epoch_ms_to_date(690613200000) == "1991-11-20"

    def test_zero_and_none_are_none(self):
        assert _epoch_ms_to_date(0) is None
        assert _epoch_ms_to_date(None) is None
        assert _epoch_ms_to_date("") is None

    def test_garbage_does_not_raise(self):
        assert _epoch_ms_to_date("not-a-date") is None


class TestStatistics:
    def test_quantiles(self):
        v = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert _quantile(v, 0.5) == 3.0
        assert _quantile(v, 0.0) == 1.0
        assert _quantile(v, 1.0) == 5.0
        assert _quantile(v, 0.25) == 2.0

    def test_quantile_interpolates(self):
        assert _quantile([10.0, 20.0], 0.5) == 15.0

    def test_quantile_handles_degenerate_input(self):
        assert _quantile([], 0.5) == 0.0
        assert _quantile([7.0], 0.5) == 7.0

    def test_outlier_trim_removes_extremes(self):
        comps = [{"v": x} for x in [100, 105, 110, 115, 120, 1000]]
        kept, dropped = _trim_outliers(comps, "v")
        assert len(dropped) == 1 and dropped[0]["v"] == 1000
        assert len(kept) == 5

    def test_small_samples_are_left_alone(self):
        comps = [{"v": x} for x in [100, 5000, 110]]
        kept, dropped = _trim_outliers(comps, "v")
        assert dropped == [] and len(kept) == 3

    def test_trim_never_leaves_too_few(self):
        """A trim that would strand fewer than three comps is abandoned."""
        comps = [{"v": x} for x in [1, 1, 1, 1, 900, 950, 1000]]
        kept, dropped = _trim_outliers(comps, "v")
        assert len(kept) >= 3


class TestRegistry:
    def test_registry_loads_and_is_shaped(self):
        reg = registry()
        assert reg["sources"], "at least one county must be configured"
        for fips, cfg in reg["sources"].items():
            assert len(fips) == 5 and fips.isdigit()
            assert cfg["urls"] and cfg["county"] and cfg["state"]
            assert "address" in cfg["fields"], "address is the one required field mapping"
            basis = cfg.get("value_basis", "market")
            assert basis == "market" or 0 < float(basis) <= 1

    def test_cuyahoga_is_configured(self):
        cfg = source_for("39035")
        assert cfg and cfg["state"] == "OH"
        assert source_for("39035") is source_for("39035")

    def test_unknown_county_is_none(self):
        assert source_for("99999") is None

    def test_non_disclosure_states(self):
        assert is_non_disclosure("TX") and is_non_disclosure("tx")
        assert not is_non_disclosure("OH")
        assert not is_non_disclosure(None)

    def test_non_disclosure_list_is_plausible(self):
        states = registry()["non_disclosure_states"]["states"]
        assert "TX" in states and "KS" in states and "UT" in states
        assert "OH" not in states and "CA" not in states
        assert len(states) == len(set(states))


class TestShaping:
    cfg = {
        "fields": {"parcel_id": "pin", "address": "addr", "market_value": "val",
                   "living_area": "area", "last_sale_price": "sale", "last_sale_date": "sdate"},
        "value_basis": "market",
    }

    def test_maps_fields_and_derives_psf(self):
        out = _shape({"pin": "1", "addr": "1 Main St", "val": 200000, "area": 1000,
                      "sale": 180000, "sdate": 690613200000}, self.cfg)
        assert out["market_value"] == 200000
        assert out["value_per_sqft"] == 200.0
        assert out["last_sale_date"] == "1991-11-20"

    def test_assessed_fraction_converts_to_market(self):
        cfg = {**self.cfg, "value_basis": 0.35}
        out = _shape({"val": 70000, "area": 1000}, cfg)
        assert out["market_value"] == 200000, "35% assessed must convert back to market"

    def test_zero_values_become_none_not_zero(self):
        out = _shape({"val": 0, "area": 0, "sale": 0}, self.cfg)
        assert out["market_value"] is None
        assert out["living_area"] is None
        assert out["last_sale_price"] is None
        assert out["value_per_sqft"] is None

    def test_missing_columns_do_not_raise(self):
        out = _shape({}, self.cfg)
        assert out["address"] is None and out["market_value"] is None


class TestTaxAndCondition:
    cfg = {
        "fields": {"address": "addr", "market_value": "val", "annual_tax": "tax",
                   "year_built": "built", "foreclosure_flag": "fc", "living_area": "area"},
        "value_basis": "market",
    }

    def test_effective_rate_comes_from_the_actual_bill(self):
        """The tax a county actually billed beats a statewide average."""
        out = _shape({"val": 68000, "tax": 1592.06}, self.cfg)
        assert out["annual_tax"] == 1592.06
        assert out["effective_tax_rate_pct"] == pytest.approx(2.341, abs=0.001)

    def test_rate_is_none_without_both_numbers(self):
        assert _shape({"val": 68000}, self.cfg)["effective_tax_rate_pct"] is None
        assert _shape({"tax": 1592}, self.cfg)["effective_tax_rate_pct"] is None

    def test_year_built_and_foreclosure_flag(self):
        out = _shape({"built": 1920, "fc": "Y"}, self.cfg)
        assert out["year_built"] == 1920
        assert out["in_foreclosure"] is True

    def test_absent_flag_is_not_a_foreclosure(self):
        for raw in (None, "", "0", "N", "None"):
            assert _shape({"fc": raw}, self.cfg)["in_foreclosure"] is False


class TestOutageHandling:
    """A county GIS server that is down must not read as 'nothing found'."""

    def _break_service(self, monkeypatch):
        from app import parcels
        def boom(url, params):
            raise DataUnavailable("Service not started")
        monkeypatch.setattr(parcels, "_query", boom)

    def test_total_failure_raises_rather_than_returning_empty(self, monkeypatch):
        from app import parcels
        self._break_service(monkeypatch)
        with pytest.raises(DataUnavailable) as e:
            parcels._radius_query(source_for("39035"), -81.6, 41.4, 100)
        assert "not responding" in str(e.value)

    def test_comps_report_the_outage_distinctly(self, monkeypatch):
        from app import parcels
        self._break_service(monkeypatch)
        out = parcels.nearby_sales("39035", 41.4, -81.6)
        assert out["reason"] == "source_unavailable"
        assert out["reason"] != "no_qualifying_sales"
        assert "not responding" in out["note"]

    def test_report_surfaces_the_outage(self, monkeypatch):
        from app import parcels
        self._break_service(monkeypatch)
        out = parcels.parcel_report("39035", 41.4, -81.6, "1 Main St", "OH", "Cuyahoga County")
        assert out["supported"] is True, "the county is configured; its server is merely down"
        assert out["available"] is False
        assert out["reason"] == "source_unavailable"
        assert out["subject"] is None

    def test_one_layer_down_is_survivable(self, monkeypatch):
        """A multi-layer county keeps working when only one layer fails."""
        from app import parcels
        cfg = {**source_for("39035"), "urls": ["http://a", "http://b"]}
        calls = []
        def flaky(url, params):
            calls.append(url)
            if url == "http://a":
                raise DataUnavailable("down")
            return [{"par_addr_all": "1 MAIN ST", "tax_market_total": 100000}]
        monkeypatch.setattr(parcels, "_query", flaky)
        rows = parcels._radius_query(cfg, -81.6, 41.4, 100)
        assert len(calls) == 2 and len(rows) == 1


class TestEndpoints:
    def test_parcel_requires_a_locator(self):
        assert client.get("/api/parcel").status_code == 400

    def test_parcel_sources_lists_configured_counties(self):
        body = client.get("/api/parcel-sources").json()
        assert any(c["county_fips"] == "39035" for c in body["configured"])
        assert "TX" in body["non_disclosure_states"]

    def test_sources_documents_the_mls_and_disclosure_gaps(self):
        gaps = client.get("/api/sources").json()["not_available_publicly"]
        text = json.dumps(gaps)
        assert "non-disclosure" in text and "MLS" in text


@pytest.mark.live
class TestLiveParcels:
    ADDR = "13301 Caine Ave, Cleveland, OH"

    def test_subject_matches_the_exact_address(self):
        d = client.get("/api/parcel", params={"address": self.ADDR}).json()
        assert d["supported"] is True
        s = d["subject"]
        assert s["match"] == "address"
        assert _street_key(s["address"]) == ("13301", "CAINE")
        assert s["market_value"] > 0 and s["living_area"] > 0
        assert s["parcel_id"]

    def test_comps_are_returned_with_defensible_statistics(self):
        d = client.get("/api/parcel", params={"address": self.ADDR}).json()
        c = d["comps"]
        assert c["available"] is True
        assert c["comps"], "comp rows must be listed, not just summarised"
        assert c["price_per_sqft_p25"] <= c["median_price_per_sqft"] <= c["price_per_sqft_p75"]
        assert c["implied_value_low"] <= c["implied_value"] <= c["implied_value_high"]
        assert "arm's-length" in c["dispersion_note"]
        for row in c["comps"]:
            assert row["last_sale_price"] >= 20000
            assert row["sale_price_per_sqft"] > 0

    def test_comps_respect_the_size_band(self):
        d = client.get("/api/parcel", params={"address": self.ADDR}).json()
        area = d["subject"]["living_area"]
        for row in d["comps"]["comps"]:
            assert 0.6 * area <= row["living_area"] <= 1.4 * area

    def test_prefill_prefers_the_parcel_over_the_zip_median(self):
        d = client.get("/api/prefill", params={"address": self.ADDR}).json()
        assert d["parcel"]["match"] == "address"
        assert d["suggested_inputs"]["purchase_price"] == d["parcel"]["market_value"]
        assert "assessor" in d["provenance"]["purchase_price"]

    def test_an_explicit_asking_price_still_wins(self):
        d = client.get("/api/prefill", params={"address": self.ADDR, "price": 90000}).json()
        assert d["suggested_inputs"]["purchase_price"] == 90000
        assert d["provenance"]["purchase_price"] == "your asking price"

    def test_unconfigured_county_explains_itself(self):
        d = client.get("/api/parcel", params={"address": "1500 Broadway, Lubbock, TX"}).json()
        assert d["supported"] is False
        assert d["reason"] == "no_adapter"
        assert d["non_disclosure_state"] is True, "Texas withholds sale prices"
        assert "county" in d["note"].lower()
