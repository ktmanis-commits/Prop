"""API-layer tests. Network-dependent tests are marked `live` and skipped by
default so the suite runs offline: `pytest -m live` to exercise them."""

import pytest
from fastapi.testclient import TestClient

from app.main import _conservative_appreciation, app

client = TestClient(app)


def test_health():
    assert client.get("/api/health").json() == {"status": "ok"}


def test_index_serves_html():
    r = client.get("/")
    assert r.status_code == 200
    assert "Property Investment Analyzer" in r.text


def test_analyze_returns_full_payload():
    r = client.post("/api/analyze", json={"purchase_price": 250_000, "monthly_rent": 2_200})
    assert r.status_code == 200
    body = r.json()
    for key in ("acquisition", "year_one", "metrics", "projection", "sensitivity", "verdict"):
        assert key in body
    assert body["verdict"]["label"]


def test_analyze_applies_defaults():
    body = client.post("/api/analyze", json={"purchase_price": 250_000}).json()
    # 20% down by default.
    assert body["acquisition"]["down_payment"] == pytest.approx(50_000)


def test_analyze_rejects_nonpositive_price():
    assert client.post("/api/analyze", json={"purchase_price": 0}).status_code == 422


def test_analyze_rejects_absurd_holding_period():
    r = client.post("/api/analyze", json={"purchase_price": 250_000, "holding_period_years": 200})
    assert r.status_code == 422


def test_market_requires_a_parameter():
    assert client.get("/api/market").status_code == 400


class TestAppreciationDefault:
    def test_caps_a_boom_market(self):
        value, basis = _conservative_appreciation({"cagr_10y": 14.8, "cagr_5y": 9.0})
        assert value == 5.0
        assert "capped" in basis

    def test_passes_through_a_normal_market(self):
        value, basis = _conservative_appreciation({"cagr_10y": 3.61, "cagr_5y": 1.0})
        assert value == 3.61
        assert "capped" not in basis

    def test_floors_a_falling_market(self):
        value, _ = _conservative_appreciation({"cagr_10y": -2.0, "cagr_5y": -4.0})
        assert value == 0.0

    def test_falls_back_to_five_year_then_default(self):
        assert _conservative_appreciation({"cagr_10y": None, "cagr_5y": 4.0})[0] == 4.0
        assert _conservative_appreciation({"cagr_10y": None, "cagr_5y": None})[0] == 3.5


@pytest.mark.live
class TestLiveData:
    def test_rates(self):
        body = client.get("/api/rates").json()
        assert 1 < body["thirty_year"]["rate"] < 20
        assert len(body["thirty_year"]["history"]["dates"]) > 50

    def test_prefill_known_zip(self):
        body = client.get("/api/prefill?zip=44105").json()
        assert body["market"]["state"] == "OH"
        s = body["suggested_inputs"]
        assert s["purchase_price"] > 0 and s["monthly_rent"] > 0
        assert 0 <= s["annual_appreciation_pct"] <= 5

    def test_prefill_price_override_scales_rent(self):
        base = client.get("/api/prefill?zip=44105").json()
        up = client.get("/api/prefill?zip=44105&price=200000").json()
        assert up["suggested_inputs"]["purchase_price"] == 200_000
        assert up["suggested_inputs"]["monthly_rent"] > base["suggested_inputs"]["monthly_rent"]

    def test_unknown_zip_is_404(self):
        assert client.get("/api/prefill?zip=00000").status_code == 404
