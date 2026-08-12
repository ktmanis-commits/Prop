"""Address resolution: routing, validation, and the live Census Geocoder."""

import pytest
from fastapi.testclient import TestClient

from app.main import _resolve, app

client = TestClient(app)


class TestLocatorRouting:
    def test_zip_passes_through_without_geocoding(self):
        zip_code, geo = _resolve("44105", None)
        assert zip_code == "44105"
        assert geo is None, "a bare ZIP must not cost a geocoder round trip"

    def test_zip_is_trimmed(self):
        assert _resolve(" 44105 ", None)[0] == "44105"

    def test_neither_locator_is_rejected(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as e:
            _resolve(None, None)
        assert e.value.status_code == 400

    def test_endpoints_require_a_locator(self):
        assert client.get("/api/prefill").status_code == 400
        assert client.get("/api/fundamentals").status_code == 400

    def test_geocode_endpoint_validates_input(self):
        assert client.get("/api/geocode?address=ab").status_code == 422

    def test_sources_documents_the_geocoder_and_tract_gap(self):
        body = client.get("/api/sources").json()
        assert any("Geocoder" in s["name"] for s in body["sources"])
        assert any("tract" in s["name"].lower() for s in body["sources"])
        assert any("tract" in g["item"].lower() for g in body["not_available_publicly"])


@pytest.mark.live
class TestLiveGeocoding:
    def test_address_resolves_to_full_geography(self):
        g = client.get("/api/geocode", params={"address": "1401 Marlowe Ave, Lakewood, OH"}).json()
        assert g["zip"] == "44107"
        assert g["county_fips"] == "39035"
        assert g["state"] == "OH"
        assert g["tract"].startswith("39035")
        assert len(g["tract"]) == 11
        assert "MARLOWE" in g["matched_address"]
        assert -90 < g["longitude"] < -70 and 35 < g["latitude"] < 45

    def test_tract_index_is_returned_when_published(self):
        g = client.get("/api/geocode", params={"address": "1401 Marlowe Ave, Lakewood, OH"}).json()
        t = g["tract_hpi"]
        assert t["tract"] == g["tract"]
        assert t["index"] > 0
        assert t["cagr_10y"] is not None
        assert len(t["history"]["years"]) == len(t["history"]["values"])

    def test_suppressed_tract_degrades_with_an_explanation(self):
        """Roughly a third of tracts have too few transactions to be published."""
        g = client.get("/api/geocode", params={"address": "1234 Euclid Ave, Cleveland, OH 44115"}).json()
        assert g["tract"] is not None
        assert g["tract_hpi"] is None
        assert "too few" in g["tract_hpi_note"]

    def test_unresolvable_address_is_404_with_guidance(self):
        r = client.get("/api/geocode", params={"address": "999999 Nowhere Rd, Atlantis, ZZ"})
        assert r.status_code == 404
        assert "ZIP" in r.json()["detail"]

    def test_prefill_by_address_matches_prefill_by_its_zip(self):
        by_addr = client.get("/api/prefill", params={"address": "1401 Marlowe Ave, Lakewood, OH"}).json()
        by_zip = client.get("/api/prefill", params={"zip": "44107"}).json()
        assert by_addr["suggested_inputs"]["purchase_price"] == by_zip["suggested_inputs"]["purchase_price"]
        # ...but the address carries geography the ZIP cannot.
        assert by_addr["geocode"]["tract"] and by_zip["geocode"] is None

    def test_address_prefers_tract_appreciation(self):
        d = client.get("/api/prefill", params={"address": "1401 Marlowe Ave, Lakewood, OH"}).json()
        assert "census tract" in d["provenance"]["appreciation"]
        assert 0 <= d["suggested_inputs"]["annual_appreciation_pct"] <= 5

    def test_fundamentals_by_address_carries_the_match(self):
        d = client.get("/api/fundamentals", params={"address": "1401 Marlowe Ave, Lakewood, OH"}).json()
        assert d["county"] == "Cuyahoga County"
        assert d["geocode"]["zip"] == "44107"
        assert d["fundamentals"]["score"] is not None
