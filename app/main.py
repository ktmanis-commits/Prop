"""FastAPI application: public market data in, investment verdict out."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import data_sources as ds
from . import market_signals as ms
from .analysis import DealInputs, analyze

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="Property Investment Analyzer",
    description="Instant residential rental-property underwriting from public market data.",
    version="1.0.0",
)


class DealRequest(BaseModel):
    purchase_price: float = Field(gt=0)
    down_payment_pct: float = 20.0
    interest_rate_pct: float = 6.5
    loan_term_years: int = 30
    closing_costs_pct: float = 3.0
    rehab_cost: float = 0.0
    after_repair_value: float = 0.0
    monthly_rent: float = 0.0
    other_monthly_income: float = 0.0
    vacancy_pct: float = 5.0
    property_tax_rate_pct: float = 1.1
    annual_insurance: float = 1500.0
    maintenance_pct: float = 5.0
    capex_pct: float = 5.0
    management_pct: float = 8.0
    monthly_hoa: float = 0.0
    monthly_utilities: float = 0.0
    other_monthly_expenses: float = 0.0
    annual_rent_growth_pct: float = 3.0
    annual_appreciation_pct: float = 3.5
    annual_expense_growth_pct: float = 2.5
    holding_period_years: int = Field(default=10, ge=1, le=40)
    selling_costs_pct: float = 7.0


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/rates")
def rates() -> dict:
    """Current 30- and 15-year fixed mortgage rates from FRED."""
    try:
        return ds.mortgage_rates()
    except ds.DataUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/market")
def market(
    zip: str | None = Query(default=None, description="5-digit ZIP code"),
    metro: str | None = Query(default=None, description="Metro name, e.g. 'Austin, TX'"),
) -> dict:
    """Home values, rents, growth rates and property-tax context for a market."""
    if not zip and not metro:
        raise HTTPException(status_code=400, detail="Provide a zip or metro parameter.")
    try:
        return ds.market_by_zip(zip) if zip else ds.market_by_metro(metro)
    except ds.DataUnavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/metros")
def metros(q: str = Query(min_length=2), limit: int = 10) -> dict:
    try:
        return {"matches": ds.search_metros(q, limit)}
    except ds.DataUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _conservative_appreciation(home_value: dict) -> tuple[float, str]:
    """Pick a defensible forward appreciation assumption.

    Trailing CAGRs are backward-looking and the 2020-2022 run-up pushes some
    markets past 10%/yr — projecting that forward for a decade would flatter
    every deal. We take the full-cycle 10-year rate when available and cap it
    at 5% (long-run US nominal home appreciation is roughly 3-4%), flooring at
    0 so a currently-falling market does not seed a negative projection. The
    UI shows the real 1/5/10-year history so the investor can override.
    """
    raw = home_value.get("cagr_10y")
    basis = "10-year trailing CAGR"
    if raw is None:
        raw = home_value.get("cagr_5y")
        basis = "5-year trailing CAGR"
    if raw is None:
        return 3.5, "long-run US average (no local history available)"
    value = max(0.0, min(5.0, raw))
    if value != round(raw, 2):
        basis += f" ({raw}%) capped to a defensible forward assumption"
    return round(value, 2), basis


@app.get("/api/prefill")
def prefill(zip: str = Query(min_length=5, max_length=5), price: float | None = None) -> dict:
    """One call that assembles a ready-to-analyze deal for a ZIP code:
    market home value, market rent, local tax rate, and today's mortgage rate.

    `price` overrides the market home value when the investor has a real
    asking price; market rent is scaled proportionally so the rent estimate
    still reflects how this property compares to the ZIP median.
    """
    try:
        mkt = ds.market_by_zip(zip)
    except ds.DataUnavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    market_value = mkt["home_value"]["latest"]
    market_rent = mkt["rent"]["latest"] if mkt.get("rent") else None
    use_price = price if price and price > 0 else market_value

    rent_estimate = None
    if market_rent and market_value:
        ratio = use_price / market_value if market_value else 1.0
        # Rent scales with size/quality but less than proportionally with price.
        rent_estimate = round(market_rent * (ratio ** 0.7))

    try:
        rate_info = ds.mortgage_rates()
        rate = rate_info["thirty_year"]["rate"]
        rate_as_of = rate_info["thirty_year"]["as_of"]
    except ds.DataUnavailable:
        rate, rate_as_of = 6.5, None

    appreciation, appreciation_basis = _conservative_appreciation(mkt["home_value"])
    rent_growth = (mkt["rent"]["cagr_5y"] if mkt.get("rent") else None)
    rent_growth = 3.0 if rent_growth is None else max(0.0, min(5.0, rent_growth))

    return {
        "market": mkt,
        "suggested_inputs": {
            "purchase_price": round(use_price),
            "monthly_rent": rent_estimate,
            "interest_rate_pct": rate,
            "property_tax_rate_pct": mkt["tax"]["effective_rate_pct"],
            "annual_appreciation_pct": appreciation,
            "annual_rent_growth_pct": round(rent_growth, 2),
        },
        "provenance": {
            "home_value": f"Zillow ZHVI, {mkt['home_value']['as_of']}",
            "rent": (f"Zillow ZORI ({mkt['rent']['level']}-level), {mkt['rent']['as_of']}"
                     if mkt.get("rent") else "unavailable"),
            "mortgage_rate": f"FRED MORTGAGE30US, {rate_as_of}" if rate_as_of else "default (FRED unavailable)",
            "property_tax": mkt["tax"]["note"],
            "appreciation": appreciation_basis,
        },
    }


@app.get("/api/fundamentals")
def fundamentals(
    zip: str = Query(min_length=5, max_length=5),
    state: str | None = Query(default=None, description="Two-letter state, for migration context"),
) -> dict:
    """Demand-side market health for the county containing this ZIP: jobs,
    listing velocity, supply pipeline, incomes and migration.

    Separate from /api/prefill because it fans out to a dozen upstream series;
    the UI loads it alongside the deal so neither blocks the other.
    """
    try:
        return ms.market_report(zip, state)
    except ds.DataUnavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/sources")
def sources() -> dict:
    """What this app reads, how fresh each source is, and what it costs.

    Exposed so the freshness of an analysis is auditable rather than implied.
    """
    return {
        "keyless": True,
        "note": ("Every source below is public and needs no API key. Figures are as "
                 "fresh as the publisher makes them — an API returns the newest "
                 "vintage instantly, but a monthly series is still monthly."),
        "sources": [
            {"name": "Freddie Mac PMMS mortgage rates", "via": "FRED", "cadence": "weekly (Thursday)",
             "lag": "same week", "used_for": "interest rate default, rate history"},
            {"name": "Zillow ZHVI home values", "via": "Zillow Research CSV", "cadence": "monthly",
             "lag": "2-3 weeks", "used_for": "purchase price default, appreciation history"},
            {"name": "Zillow ZORI market rents", "via": "Zillow Research CSV", "cadence": "monthly",
             "lag": "2-3 weeks", "used_for": "rent default, rent growth"},
            {"name": "Realtor.com listing metrics", "via": "FRED (county)", "cadence": "monthly",
             "lag": "~2 weeks", "used_for": "days on market, inventory, price cuts, $/sqft"},
            {"name": "BLS Local Area Unemployment Statistics", "via": "FRED (county)", "cadence": "monthly",
             "lag": "~3 weeks", "used_for": "employment growth, unemployment rate"},
            {"name": "FHFA House Price Index", "via": "FRED (county)", "cadence": "annual",
             "lag": "~1 quarter", "used_for": "transaction-based price trend"},
            {"name": "BEA per-capita personal income", "via": "FRED (county)", "cadence": "annual",
             "lag": "~1 year", "used_for": "income growth, rent affordability ceiling"},
            {"name": "Census Building Permits Survey", "via": "FRED (county)", "cadence": "annual",
             "lag": "~1 quarter", "used_for": "incoming supply"},
            {"name": "Census ZCTA-to-county crosswalk", "via": "census.gov", "cadence": "decennial",
             "lag": "static", "used_for": "resolving a ZIP to its county"},
            {"name": "U-Haul Growth Index", "via": "bundled snapshot", "cadence": "annual press release",
             "lag": "see migration.json", "used_for": "state migration direction"},
            {"name": "Census net domestic migration", "via": "bundled snapshot", "cadence": "annual",
             "lag": "see migration.json", "used_for": "measured state migration"},
        ],
        "not_available_publicly": [
            {"item": "Parcel-level sold comps", "why": "MLS-licensed; no national public feed. "
             "Use the county assessor's recorded deed transfers or a paid comps API."},
            {"item": "County assessor parcel and tax records", "why": "No national API — every county "
             "publishes differently. Many expose ArcGIS REST services; a per-county adapter is needed."},
            {"item": "Daily mortgage rates", "why": "PMMS is a weekly survey. Daily pricing is commercial."},
        ],
    }


@app.post("/api/analyze")
def analyze_deal(req: DealRequest) -> dict:
    return analyze(DealInputs(**req.model_dump()))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
