"""FastAPI application: public market data in, investment verdict out."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import data_sources as ds
from . import insurance as ins
from . import market_signals as ms
from . import parcels as pc
from . import tax_rates as tr
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
    rehab_financed_pct: float = Field(default=0.0, ge=0, le=100)
    interest_only_years: int = Field(default=0, ge=0, le=40)
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


def _resolve(zip: str | None, address: str | None) -> tuple[str, dict | None]:
    """Turn whichever locator the caller supplied into a ZIP plus, when an
    address was given, the geocode result that produced it."""
    if address:
        geo = ds.geocode(address)
        if not geo.get("zip"):
            raise HTTPException(status_code=404,
                                detail=f"Geocoder matched '{geo['matched_address']}' but returned no ZIP.")
        return geo["zip"], geo
    if zip:
        return zip.strip(), None
    raise HTTPException(status_code=400, detail="Provide either an address or a zip.")


@app.get("/api/geocode")
def geocode(address: str = Query(min_length=4)) -> dict:
    """Resolve a street address to a normalized address, ZIP, county and census
    tract, with the tract-level price index when FHFA publishes one."""
    try:
        geo = ds.geocode(address)
    except ds.DataUnavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if geo.get("tract"):
        try:
            geo["tract_hpi"] = ds.tract_hpi(geo["tract"])
        except ds.DataUnavailable as exc:
            geo["tract_hpi"] = None
            geo["tract_hpi_note"] = str(exc)
    return geo


@app.get("/api/prefill")
def prefill(
    zip: str | None = Query(default=None, min_length=5, max_length=5),
    address: str | None = Query(default=None, description="Street address; resolved via Census Geocoder"),
    price: float | None = None,
) -> dict:
    """One call that assembles a ready-to-analyze deal for a ZIP code:
    market home value, market rent, local tax rate, and today's mortgage rate.

    `price` overrides the market home value when the investor has a real
    asking price; market rent is scaled proportionally so the rent estimate
    still reflects how this property compares to the ZIP median.
    """
    try:
        resolved_zip, geo = _resolve(zip, address)
        mkt = ds.market_by_zip(resolved_zip)
    except ds.DataUnavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # An exact address buys census-tract resolution, which is a far tighter
    # read on appreciation than the ZIP median — when FHFA publishes the tract.
    tract = None
    if geo and geo.get("tract"):
        try:
            tract = ds.tract_hpi(geo["tract"])
        except ds.DataUnavailable:
            tract = None

    # The county's own record of this parcel beats a ZIP median as a price
    # default. Comps and discovery live in /api/parcel so this stays quick.
    parcel = None
    if geo and geo.get("county_fips"):
        try:
            parcel = pc.subject_property(geo["county_fips"], geo["latitude"],
                                         geo["longitude"], geo["matched_address"],
                                         geo.get("components"))
        except Exception:
            parcel = None

    market_value = mkt["home_value"]["latest"]
    market_rent = mkt["rent"]["latest"] if mkt.get("rent") else None
    parcel_value = (parcel or {}).get("market_value") if (parcel or {}).get("match") == "address" else None
    use_price = price if price and price > 0 else (parcel_value or market_value)

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

    if tract and (tract["cagr_10y"] is not None or tract["cagr_5y"] is not None):
        appreciation, appreciation_basis = _conservative_appreciation({
            "cagr_10y": tract["cagr_10y"], "cagr_5y": tract["cagr_5y"]})
        appreciation_basis = f"census tract {tract['tract']} {appreciation_basis}"
    else:
        appreciation, appreciation_basis = _conservative_appreciation(mkt["home_value"])
    rent_growth = (mkt["rent"]["cagr_5y"] if mkt.get("rent") else None)
    rent_growth = 3.0 if rent_growth is None else max(0.0, min(5.0, rent_growth))

    # Property tax, best source first:
    #   1. the amount the county actually billed on this parcel
    #   2. the sum of the jurisdictions that tax it (county + city + school + special)
    #   3. the statewide average, which is a poor stand-in where school rates vary
    parcel_tax_rate = (parcel or {}).get("effective_tax_rate_pct") if parcel_value else None
    jurisdictions = None
    if geo and geo.get("county_fips") and not parcel_tax_rate:
        try:
            jurisdictions = tr.effective_rate(geo["county_fips"], geo.get("latitude"),
                                              geo.get("longitude"),
                                              (geo.get("components") or {}).get("city"))
        except Exception:
            jurisdictions = None

    if parcel_tax_rate:
        tax_rate = parcel_tax_rate
        tax_basis = (f"{parcel['county']} billed {parcel['annual_tax']:,.0f} on this parcel in "
                     f"tax year {parcel.get('tax_year')} — {parcel_tax_rate}% of its market value")
    elif jurisdictions and jurisdictions["complete"]:
        tax_rate = jurisdictions["effective_rate_pct"]
        tax_basis = (f"{' + '.join(u['name'] for u in jurisdictions['units'])} "
                     f"= {tax_rate}% ({jurisdictions['tax_year']} adopted rates, "
                     f"{jurisdictions['source']})")
    else:
        tax_rate = mkt["tax"]["effective_rate_pct"]
        tax_basis = mkt["tax"]["note"]

    insurance = ins.for_zip(resolved_zip)

    homestead = tr.homestead_warning(
        (geo or {}).get("state"), (parcel or {}).get("exemptions"),
        (parcel or {}).get("market_value"), use_price)

    return {
        "geocode": geo,
        "tract_hpi": tract,
        "parcel": parcel,
        "tax_jurisdictions": jurisdictions,
        "insurance": insurance,
        "homestead": homestead,
        "market": mkt,
        "suggested_inputs": {
            "purchase_price": round(use_price),
            "monthly_rent": rent_estimate,
            "interest_rate_pct": rate,
            "property_tax_rate_pct": tax_rate,
            "annual_insurance": (insurance or {}).get("annual_premium") or 1500,
            "annual_appreciation_pct": appreciation,
            "annual_rent_growth_pct": round(rent_growth, 2),
        },
        "provenance": {
            "home_value": f"Zillow ZHVI, {mkt['home_value']['as_of']}",
            "rent": (f"Zillow ZORI ({mkt['rent']['level']}-level), {mkt['rent']['as_of']}"
                     if mkt.get("rent") else "unavailable"),
            "mortgage_rate": f"FRED MORTGAGE30US, {rate_as_of}" if rate_as_of else "default (FRED unavailable)",
            "property_tax": tax_basis,
            "insurance": (f"{insurance['source']}, {insurance['data_year']} average for ZIP "
                          f"{insurance['zip']}" if insurance else "generic default"),
            "appreciation": appreciation_basis,
            "purchase_price": (
                "your asking price" if price and price > 0 else
                (f"{parcel['county']} assessor market value"
                 + (f", tax year {parcel['tax_year']}" if parcel.get("tax_year") else ""))
                if parcel_value else f"Zillow ZHVI ZIP median, {mkt['home_value']['as_of']}"),
        },
    }


@app.get("/api/fundamentals")
def fundamentals(
    zip: str | None = Query(default=None, min_length=5, max_length=5),
    address: str | None = Query(default=None, description="Street address; resolved via Census Geocoder"),
    state: str | None = Query(default=None, description="Two-letter state, for migration context"),
) -> dict:
    """Demand-side market health around this property: jobs, listing velocity,
    supply pipeline, incomes and migration.

    Separate from /api/prefill because it fans out to a dozen upstream series;
    the UI loads it alongside the deal so neither blocks the other.
    """
    try:
        resolved_zip, geo = _resolve(zip, address)
        report = ms.market_report(resolved_zip, state or (geo or {}).get("state"))
    except ds.DataUnavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if geo:
        report["geocode"] = geo
        report["tract_hpi"] = None
        if geo.get("tract"):
            try:
                report["tract_hpi"] = ds.tract_hpi(geo["tract"])
            except ds.DataUnavailable as exc:
                report["tract_hpi_note"] = str(exc)
    return report


@app.get("/api/parcel")
def parcel(
    address: str | None = Query(default=None, description="Street address of the subject property"),
    lat: float | None = None,
    lon: float | None = None,
    county_fips: str | None = None,
) -> dict:
    """County assessor record for the subject property plus nearby recorded sales.

    Requires an address (or explicit coordinates and county) because parcel data
    is inherently property-level. Counties without a configured adapter get an
    explanation and candidate layers rather than an empty result.
    """
    geo = None
    if address:
        try:
            geo = ds.geocode(address)
        except ds.DataUnavailable as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        lat, lon = geo["latitude"], geo["longitude"]
        county_fips = geo["county_fips"]
    if lat is None or lon is None or not county_fips:
        raise HTTPException(status_code=400,
                            detail="Provide an address, or lat, lon and county_fips.")
    try:
        report = pc.parcel_report(county_fips, lat, lon,
                                  (geo or {}).get("matched_address") or address,
                                  (geo or {}).get("state"), (geo or {}).get("county"),
                                  (geo or {}).get("components"))
    except ds.DataUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if geo:
        report["geocode"] = geo
    return report


@app.get("/api/parcel-sources")
def parcel_sources() -> dict:
    """Which counties have a parcel adapter, and which states withhold sale prices."""
    reg = pc.registry()
    return {
        "configured": [
            {"county_fips": fips, "county": cfg["county"], "state": cfg["state"],
             "verified": cfg.get("verified"), "attribution": cfg.get("attribution")}
            for fips, cfg in sorted(reg.get("sources", {}).items())
        ],
        "non_disclosure_states": reg.get("non_disclosure_states", {}).get("states", []),
        "note": ("Assessor records have no national API. Each county publishes its own service, "
                 "so support is added one county at a time in data/county_parcels.json. "
                 "GET /api/parcel for an unconfigured county returns candidate layers found "
                 "through the ArcGIS Online search API."),
    }


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
            {"name": "Census Geocoder", "via": "geocoding.geo.census.gov", "cadence": "continuous",
             "lag": "live", "used_for": "resolving a street address to ZIP, county and census tract"},
            {"name": "County assessor parcel records", "via": "per-county ArcGIS services",
             "cadence": "varies (typically daily to annual)", "lag": "varies",
             "used_for": "subject property value, size, land use, last sale, and sold comps"},
            {"name": "FHFA House Price Index, census tract", "via": "fhfa.gov", "cadence": "annual",
             "lag": "~1 quarter", "used_for": "neighborhood-level appreciation when an address is given"},
            {"name": "Census ZCTA-to-county crosswalk", "via": "census.gov", "cadence": "decennial",
             "lag": "static", "used_for": "resolving a ZIP to its county"},
            {"name": "U-Haul Growth Index", "via": "bundled snapshot", "cadence": "annual press release",
             "lag": "see migration.json", "used_for": "state migration direction"},
            {"name": "Census net domestic migration", "via": "bundled snapshot", "cadence": "annual",
             "lag": "see migration.json", "used_for": "measured state migration"},
        ],
        "not_available_publicly": [
            {"item": "Sold comps in non-disclosure states", "why": "Twelve states do not make sale "
             "prices public record (" + ", ".join(pc.registry()
                .get("non_disclosure_states", {}).get("states", [])) + "). Assessed values and "
             "property characteristics are available there; recorded sale prices are not."},
            {"item": "County assessor records outside configured counties", "why": "No national API — "
             "every county publishes differently. See /api/parcel-sources for what is configured; "
             "an unconfigured county returns candidate ArcGIS layers to map."},
            {"item": "MLS listing and sold data", "why": "License-restricted with no public feed. "
             "This app uses recorded deed transfers from the assessor instead, which cover fewer "
             "attributes and cannot flag a distressed or non-arm's-length sale."},
            {"item": "Price index for every census tract", "why": "FHFA suppresses tracts with too few "
             "recorded transactions, so roughly a third of tracts have no published index. Those fall "
             "back to the ZIP-level series."},
            {"item": "Daily mortgage rates", "why": "PMMS is a weekly survey. Daily pricing is commercial."},
        ],
    }


@app.post("/api/analyze")
def analyze_deal(req: DealRequest) -> dict:
    return analyze(DealInputs(**req.model_dump()))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
