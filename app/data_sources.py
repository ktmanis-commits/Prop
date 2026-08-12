"""Public-data fetchers: FRED mortgage rates and Zillow Research market data.

All downloads are cached on disk under data/cache/ and refreshed after
CACHE_TTL_HOURS. Parsed DataFrames are additionally memoized in memory so a
running server pays the parse cost once. Every function degrades gracefully:
on network failure it serves a stale cache if one exists, else raises
DataUnavailable so the API can tell the client to fall back to manual input.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_DIR = DATA_DIR / "cache"
CACHE_TTL_HOURS = 24

# Cache lifetimes chosen to match how often each source actually publishes.
# Re-downloading a monthly series hourly costs bandwidth and buys nothing.
TTL_WEEKLY = 12        # Freddie Mac PMMS posts Thursdays
TTL_MONTHLY = 24       # Zillow, Realtor.com, BLS monthly series
TTL_ANNUAL = 24 * 7    # FHFA HPI, BEA income, building permits
TTL_STATIC = 24 * 90   # geographic crosswalks change with the decennial census

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
ZILLOW = {
    "metro_zhvi": "https://files.zillowstatic.com/research/public_csvs/zhvi/Metro_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv",
    "metro_zori": "https://files.zillowstatic.com/research/public_csvs/zori/Metro_zori_uc_sfrcondomfr_sm_month.csv",
    "zip_zhvi": "https://files.zillowstatic.com/research/public_csvs/zhvi/Zip_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv",
    "zip_zori": "https://files.zillowstatic.com/research/public_csvs/zori/Zip_zori_uc_sfrcondomfr_sm_month.csv",
}

_frames: dict[str, pd.DataFrame] = {}


class DataUnavailable(Exception):
    pass


def _cache_path(name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{name}.csv"


def _fresh(path: Path, ttl_hours: float = CACHE_TTL_HOURS) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < ttl_hours * 3600


def _download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(".tmp")
    # www2.census.gov sits behind a WAF that rejects requests with no Referer.
    headers = {"Referer": "https://www.census.gov/", "User-Agent": "property-investment-analyzer/1.0"}
    with httpx.stream("GET", url, timeout=120, follow_redirects=True, headers=headers) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    tmp.replace(dest)


def _fetch_csv(name: str, url: str, ttl_hours: float = CACHE_TTL_HOURS) -> Path:
    path = _cache_path(name)
    if _fresh(path, ttl_hours):
        return path
    try:
        _download(url, path)
    except Exception as exc:
        if path.exists():  # serve stale rather than fail
            return path
        raise DataUnavailable(f"Could not download {name}: {exc}") from exc
    return path


def fred_series(series_id: str, ttl_hours: float = TTL_MONTHLY) -> tuple[list[str], list[float]]:
    """Fetch one FRED series as (dates, values) via the keyless CSV endpoint.

    FRED mirrors BLS, Census, BEA, FHFA and Realtor.com series, which lets this
    app read all of them without an API key. Missing observations arrive as "."
    and are dropped. Raises DataUnavailable when the series does not exist —
    many county-level series are absent for small or rural counties.
    """
    path = _fetch_csv(f"fred_{series_id}", FRED_CSV.format(series=series_id), ttl_hours)
    try:
        df = pd.read_csv(path)
    except Exception as exc:
        raise DataUnavailable(f"FRED series {series_id} unreadable: {exc}") from exc
    if df.shape[1] < 2 or "observation_date" not in df.columns[0]:
        path.unlink(missing_ok=True)  # cached an HTML error page; do not keep it
        raise DataUnavailable(f"FRED has no series {series_id}.")
    df.columns = ["date", "value"]
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna()
    if df.empty:
        raise DataUnavailable(f"FRED series {series_id} has no observations.")
    return df["date"].astype(str).tolist(), df["value"].astype(float).tolist()


# ---------------------------------------------------------------- FRED rates

def mortgage_rates() -> dict:
    """Latest 30y/15y fixed rates plus two years of weekly history."""
    out: dict = {"source": "FRED (Freddie Mac Primary Mortgage Market Survey)"}
    for key, series in (("thirty_year", "MORTGAGE30US"), ("fifteen_year", "MORTGAGE15US")):
        path = _fetch_csv(f"fred_{series}", FRED_CSV.format(series=series), TTL_WEEKLY)
        df = pd.read_csv(path)
        df.columns = ["date", "rate"]
        df["rate"] = pd.to_numeric(df["rate"], errors="coerce")
        df = df.dropna()
        latest = df.iloc[-1]
        year_ago = df[df["date"] <= _shift_date(str(latest["date"]), years=1)]
        hist = df.tail(104)  # ~2 years of weekly observations
        out[key] = {
            "rate": float(latest["rate"]),
            "as_of": str(latest["date"]),
            "year_ago": float(year_ago.iloc[-1]["rate"]) if len(year_ago) else None,
            "history": {
                "dates": hist["date"].tolist(),
                "rates": hist["rate"].round(2).tolist(),
            },
        }
    return out


def _shift_date(iso: str, years: int) -> str:
    y, rest = iso[:4], iso[4:]
    return f"{int(y) - years}{rest}"


# ------------------------------------------------------------- Zillow frames

def _load_frame(name: str) -> pd.DataFrame:
    if name in _frames:
        return _frames[name]
    path = _fetch_csv(name, ZILLOW[name])
    df = pd.read_csv(path)
    _frames[name] = df
    return df


def _series_from_row(row: pd.Series, months: int = 132) -> tuple[list[str], list[float]]:
    """Extract the trailing monthly time series (date-named columns) from a
    Zillow wide-format row, dropping leading NaNs."""
    date_cols = [c for c in row.index if len(c) == 10 and c[4] == "-" and c[7] == "-"]
    date_cols = date_cols[-months:]
    dates, values = [], []
    for c in date_cols:
        v = row[c]
        if pd.notna(v):
            dates.append(c)
            values.append(round(float(v), 2))
    return dates, values


def _cagr(dates: list[str], values: list[float], years: int) -> float | None:
    """Compound annual growth over the trailing `years`, if enough history."""
    if not values:
        return None
    target = _shift_date(dates[-1], years)
    past = [(d, v) for d, v in zip(dates, values) if d <= target]
    if not past:
        return None
    d0, v0 = past[-1]
    if v0 <= 0:
        return None
    span_years = (_date_ord(dates[-1]) - _date_ord(d0)) / 365.25
    if span_years < years * 0.9:
        return None
    return round(((values[-1] / v0) ** (1 / span_years) - 1) * 100, 2)


def _date_ord(iso: str) -> int:
    y, m, d = int(iso[:4]), int(iso[5:7]), int(iso[8:10])
    return y * 365 + m * 30 + d


def _stats(dates: list[str], values: list[float]) -> dict:
    return {
        "latest": values[-1] if values else None,
        "as_of": dates[-1] if dates else None,
        "cagr_1y": _cagr(dates, values, 1),
        "cagr_3y": _cagr(dates, values, 3),
        "cagr_5y": _cagr(dates, values, 5),
        "cagr_10y": _cagr(dates, values, 10),
        "history": {"dates": dates, "values": values},
    }


def market_by_zip(zip_code: str) -> dict:
    """ZHVI + ZORI stats for a ZIP code, with metro fallback for rent."""
    zip_code = zip_code.strip().zfill(5)
    zhvi = _load_frame("zip_zhvi")
    row = zhvi[zhvi["RegionName"].astype(str).str.zfill(5) == zip_code]
    if row.empty:
        raise DataUnavailable(f"ZIP {zip_code} not found in Zillow home-value data.")
    row = row.iloc[0]
    dates, values = _series_from_row(row)
    result = {
        "level": "zip",
        "zip": zip_code,
        "city": row.get("City"),
        "state": row.get("State"),
        "metro": row.get("Metro"),
        "county": row.get("CountyName"),
        "home_value": _stats(dates, values),
        "source": "Zillow Research ZHVI / ZORI (public CSVs)",
    }

    zori = _load_frame("zip_zori")
    rrow = zori[zori["RegionName"].astype(str).str.zfill(5) == zip_code]
    rent_level = "zip"
    if rrow.empty and isinstance(row.get("Metro"), str):
        mz = _load_frame("metro_zori")
        rrow = mz[mz["RegionName"] == row["Metro"]]
        rent_level = "metro"
    if not rrow.empty:
        rd, rv = _series_from_row(rrow.iloc[0])
        result["rent"] = _stats(rd, rv)
        result["rent"]["level"] = rent_level
    else:
        result["rent"] = None

    hv = result["home_value"]["latest"]
    rent = result["rent"]["latest"] if result["rent"] else None
    result["price_to_rent"] = round(hv / (rent * 12), 1) if hv and rent else None
    result["tax"] = state_tax_rate(result.get("state"))
    return result


def market_by_metro(query: str) -> dict:
    zhvi = _load_frame("metro_zhvi")
    match = zhvi[zhvi["RegionName"].str.contains(query, case=False, na=False)]
    if match.empty:
        raise DataUnavailable(f"No metro matching '{query}'.")
    row = match.iloc[0]
    dates, values = _series_from_row(row)
    result = {
        "level": "metro",
        "metro": row["RegionName"],
        "state": row.get("StateName"),
        "home_value": _stats(dates, values),
        "source": "Zillow Research ZHVI / ZORI (public CSVs)",
    }
    zori = _load_frame("metro_zori")
    rrow = zori[zori["RegionName"] == row["RegionName"]]
    if not rrow.empty:
        rd, rv = _series_from_row(rrow.iloc[0])
        result["rent"] = _stats(rd, rv)
        result["rent"]["level"] = "metro"
    else:
        result["rent"] = None
    hv = result["home_value"]["latest"]
    rent = result["rent"]["latest"] if result["rent"] else None
    result["price_to_rent"] = round(hv / (rent * 12), 1) if hv and rent else None
    result["tax"] = state_tax_rate(result.get("state"))
    return result


def search_metros(query: str, limit: int = 10) -> list[str]:
    zhvi = _load_frame("metro_zhvi")
    match = zhvi[zhvi["RegionName"].str.contains(query, case=False, na=False)]
    return match["RegionName"].head(limit).tolist()


# ------------------------------------------------- ZIP -> county geography

ZCTA_COUNTY_URL = ("https://www2.census.gov/geo/docs/maps-data/data/rel2020/"
                   "zcta520/tab20_zcta520_county20_natl.txt")

# Census Population Estimates Program, county vintage 2025. The modern PEP has
# no API (the api.census.gov pep/components endpoint stops at vintage 2019 and
# now requires a key), but the flat file is public and current.
PEP_COUNTY_URL = ("https://www2.census.gov/programs-surveys/popest/datasets/"
                  "2020-2025/counties/totals/co-est2025-alldata.csv")

_zcta_map: dict[str, tuple[str, str]] | None = None


def zip_to_county(zip_code: str) -> tuple[str, str]:
    """Map a ZIP to its (county FIPS, county name) using the Census ZCTA-to-county
    relationship file. A ZIP that straddles counties resolves to the county
    holding the largest share of its land area."""
    global _zcta_map
    if _zcta_map is None:
        path = _fetch_csv("zcta_county", ZCTA_COUNTY_URL, TTL_STATIC)
        df = pd.read_csv(path, sep="|", dtype=str, encoding="utf-8-sig",
                         usecols=["GEOID_ZCTA5_20", "GEOID_COUNTY_20",
                                  "NAMELSAD_COUNTY_20", "AREALAND_PART"])
        df = df.dropna(subset=["GEOID_ZCTA5_20", "GEOID_COUNTY_20"])
        df["AREALAND_PART"] = pd.to_numeric(df["AREALAND_PART"], errors="coerce").fillna(0)
        df = df.sort_values("AREALAND_PART", ascending=False).drop_duplicates("GEOID_ZCTA5_20")
        _zcta_map = {
            r.GEOID_ZCTA5_20.zfill(5): (r.GEOID_COUNTY_20.zfill(5), r.NAMELSAD_COUNTY_20)
            for r in df.itertuples()
        }
    hit = _zcta_map.get(zip_code.strip().zfill(5))
    if not hit:
        raise DataUnavailable(f"No county mapping for ZIP {zip_code}.")
    return hit


# ------------------------------------------- ZIP-level listings and rents

REALTOR_ZIP_URL = ("https://econdata.s3-us-west-2.amazonaws.com/Reports/Core/"
                   "RDC_Inventory_Core_Metrics_Zip.csv")
# HUD Small Area Fair Market Rents: 40th-percentile standard-quality rents by
# ZIP and bedroom count. Methodologically independent of Zillow's ZORI, which
# makes it a genuine second opinion rather than a second helping of the same.
HUD_SAFMR_URL = ("https://www.huduser.gov/portal/datasets/fmr/fmr2026/"
                 "fy2026_safmrs_revised.xlsx")
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_realtor: pd.DataFrame | None = None
_safmr: pd.DataFrame | None = None


def realtor_zip(zip_code: str) -> dict:
    """Current-month Realtor.com listing metrics for a ZIP, with year-over-year
    changes. Finer than the county series and published about two weeks after
    month end."""
    global _realtor
    if _realtor is None:
        path = _fetch_csv("realtor_zip", REALTOR_ZIP_URL, TTL_MONTHLY)
        _realtor = pd.read_csv(path, dtype={"postal_code": str})
        _realtor["postal_code"] = _realtor["postal_code"].str.zfill(5)
    row = _realtor[_realtor["postal_code"] == zip_code.strip().zfill(5)]
    if row.empty:
        raise DataUnavailable(f"No Realtor.com listing data for ZIP {zip_code}.")
    r = row.iloc[0]

    def num(col):
        v = r.get(col)
        return None if pd.isna(v) else float(v)

    month = str(int(r["month_date_yyyymm"]))
    return {
        "zip": r["postal_code"],
        "name": r.get("zip_name"),
        "as_of": f"{month[:4]}-{month[4:]}",
        "median_list_price": num("median_listing_price"),
        "median_list_price_yoy_pct": (num("median_listing_price_yy") or 0) * 100
            if num("median_listing_price_yy") is not None else None,
        "days_on_market": num("median_days_on_market"),
        "days_on_market_yoy_pct": (num("median_days_on_market_yy") or 0) * 100
            if num("median_days_on_market_yy") is not None else None,
        "active_listings": num("active_listing_count"),
        "active_listings_yoy_pct": (num("active_listing_count_yy") or 0) * 100
            if num("active_listing_count_yy") is not None else None,
        "new_listings": num("new_listing_count"),
        "price_cut_share_pct": (num("price_reduced_share") or 0) * 100
            if num("price_reduced_share") is not None else None,
        "price_per_sqft": num("median_listing_price_per_square_foot"),
        "median_sqft": num("median_square_feet"),
        # Pending divided by active: how much of the market is already spoken for.
        "pending_ratio": num("pending_ratio"),
        "source": "Realtor.com Research, ZIP-level core inventory metrics",
    }


def hud_safmr(zip_code: str) -> dict:
    """HUD Small Area Fair Market Rents by bedroom count for a ZIP."""
    global _safmr
    if _safmr is None:
        path = _cache_path("hud_safmr")
        path = path.with_suffix(".xlsx")
        if not _fresh(path, TTL_ANNUAL):
            try:
                _download_ua(HUD_SAFMR_URL, path)
            except Exception as exc:
                if not path.exists():
                    raise DataUnavailable(f"Could not download HUD SAFMR: {exc}") from exc
        df = pd.read_excel(path, dtype={"ZIP Code": str}, engine="openpyxl")
        df.columns = [" ".join(str(c).split()) for c in df.columns]
        df["ZIP Code"] = df["ZIP Code"].astype(str).str.zfill(5)
        _safmr = df
    row = _safmr[_safmr["ZIP Code"] == zip_code.strip().zfill(5)]
    if row.empty:
        raise DataUnavailable(f"No HUD Small Area FMR for ZIP {zip_code}.")
    r = row.iloc[0]
    beds = {}
    for n in range(5):
        v = r.get(f"SAFMR {n}BR")
        if pd.notna(v):
            beds[str(n)] = int(v)
    return {
        "zip": r["ZIP Code"],
        "area": r.get("HUD Fair Market Rent Area Name"),
        "by_bedroom": beds,
        "year": "FY2026",
        "source": "HUD Small Area Fair Market Rents FY2026",
        "note": ("HUD's FMR is the 40th percentile of standard-quality rents — a "
                 "conservative floor for a market-rate unit in good condition, not a "
                 "median. Computed independently of Zillow, so agreement between the "
                 "two is real corroboration."),
    }


def _download_ua(url: str, dest: Path) -> None:
    """Download with a browser User-Agent. huduser.gov answers 202 with an empty
    body when the UA looks automated, which reads as a dead link if unhandled."""
    tmp = dest.with_suffix(".tmp")
    with httpx.stream("GET", url, timeout=180, follow_redirects=True,
                      headers={"User-Agent": BROWSER_UA, "Accept": "*/*"}) as r:
        r.raise_for_status()
        if r.headers.get("content-length") == "0":
            raise DataUnavailable("empty response (blocked)")
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    tmp.replace(dest)


# ------------------------------------------------------ address geocoding

# The Census Geocoder is free, keyless, and authoritative for US addresses. It
# returns a normalized address plus the full geography stack, which is what
# lets an exact address resolve to a census tract instead of just a ZIP.
GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"

# FHFA annual house price index at census-tract resolution — the finest public
# price signal available. FHFA suppresses tracts with too few transactions, so
# coverage is partial and callers must be ready to fall back to the county.
FHFA_TRACT_URL = "https://www.fhfa.gov/hpi/download/annual/hpi_at_tract.csv"

_tract_hpi_cache: dict[str, dict] = {}


def geocode(address: str) -> dict:
    """Resolve a street address to a normalized address, coordinates, ZIP,
    county FIPS and census tract."""
    params = {
        "address": address,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "format": "json",
    }
    try:
        r = httpx.get(GEOCODER_URL, params=params, timeout=45,
                      headers={"User-Agent": "property-investment-analyzer/1.0"})
        r.raise_for_status()
        matches = r.json()["result"]["addressMatches"]
    except Exception as exc:
        raise DataUnavailable(f"Address lookup failed: {exc}") from exc
    if not matches:
        raise DataUnavailable(
            f"No match for '{address}'. Try including city and state, or enter the ZIP code.")

    m = matches[0]
    geo = m.get("geographies", {})

    def first(layer: str, key: str) -> str | None:
        rows = geo.get(layer) or []
        return rows[0].get(key) if rows else None

    comp = m.get("addressComponents", {})
    county_fips = first("Counties", "GEOID")
    return {
        "query": address,
        "matched_address": m.get("matchedAddress"),
        "latitude": m["coordinates"]["y"],
        "longitude": m["coordinates"]["x"],
        "zip": comp.get("zip"),
        "city": (comp.get("city") or "").title() or None,
        "state": comp.get("state"),
        "county_fips": county_fips,
        "county": first("Counties", "NAME"),
        "tract": first("Census Tracts", "GEOID"),
        "block": first("2020 Census Blocks", "GEOID"),
        "other_matches": [x.get("matchedAddress") for x in matches[1:4]],
        "source": "US Census Bureau Geocoder (Public_AR_Current)",
    }


def tract_hpi(tract_geoid: str) -> dict:
    """FHFA annual house price index for one census tract.

    Scans the 89 MB national file in chunks rather than holding it in memory;
    results are memoized per tract, so only the first lookup pays the ~1.5s.
    """
    if tract_geoid in _tract_hpi_cache:
        return _tract_hpi_cache[tract_geoid]
    path = _fetch_csv("fhfa_tract", FHFA_TRACT_URL, TTL_ANNUAL)
    frames = []
    for chunk in pd.read_csv(path, dtype={"tract": str}, chunksize=400_000):
        hit = chunk[chunk["tract"] == tract_geoid]
        if not hit.empty:
            frames.append(hit)
    if not frames:
        raise DataUnavailable(
            f"FHFA does not publish a tract-level index for {tract_geoid} "
            "(too few recorded transactions).")
    df = pd.concat(frames).sort_values("year")
    df = df[pd.to_numeric(df["hpi"], errors="coerce").notna()]
    years = df["year"].astype(int).tolist()
    values = df["hpi"].astype(float).tolist()

    def cagr(n: int) -> float | None:
        if len(values) <= n or values[-1 - n] <= 0:
            return None
        return round(((values[-1] / values[-1 - n]) ** (1 / n) - 1) * 100, 2)

    latest_change = df["annual_change"].iloc[-1]
    out = {
        "tract": tract_geoid,
        "as_of": years[-1],
        "index": round(values[-1], 2),
        "change_1y_pct": round(float(latest_change), 2) if pd.notna(latest_change) else None,
        "cagr_5y": cagr(5),
        "cagr_10y": cagr(10),
        "history": {"years": years[-25:], "values": [round(v, 2) for v in values[-25:]]},
        "source": "FHFA annual house price index, census-tract level",
    }
    _tract_hpi_cache[tract_geoid] = out
    return out


_pep: pd.DataFrame | None = None


def county_population(fips: str) -> dict:
    """Population estimates and measured net domestic migration for a county.

    Returns the latest population, its compound growth since 2020, and net
    domestic migration for the latest year both in people and per 1,000
    residents (Census publishes the rate directly as RDOMESTICMIG).
    """
    global _pep
    if _pep is None:
        path = _fetch_csv("pep_county", PEP_COUNTY_URL, TTL_ANNUAL)
        _pep = pd.read_csv(path, encoding="latin-1", dtype={"STATE": str, "COUNTY": str})
        _pep["fips"] = _pep["STATE"].str.zfill(2) + _pep["COUNTY"].str.zfill(3)
    row = _pep[_pep["fips"] == fips.zfill(5)]
    if row.empty:
        raise DataUnavailable(f"No Census population estimates for county {fips}.")
    row = row.iloc[0]

    years = sorted(int(c.replace("POPESTIMATE", "")) for c in _pep.columns
                   if c.startswith("POPESTIMATE") and c[-4:].isdigit())
    base_year, last_year = years[0], years[-1]
    base, latest = float(row[f"POPESTIMATE{base_year}"]), float(row[f"POPESTIMATE{last_year}"])
    span = last_year - base_year
    cagr = round(((latest / base) ** (1 / span) - 1) * 100, 2) if base > 0 and span else None

    mig = row.get(f"DOMESTICMIG{last_year}")
    rate = row.get(f"RDOMESTICMIG{last_year}")
    return {
        "county": row["CTYNAME"],
        "state": row["STNAME"],
        "population": int(latest),
        "as_of": str(last_year),
        "growth_pct_per_year": cagr,
        "growth_since": str(base_year),
        "net_domestic_migration": int(mig) if pd.notna(mig) else None,
        "net_domestic_migration_per_1k": round(float(rate), 2) if pd.notna(rate) else None,
        "source": f"Census Population Estimates Program, county vintage {last_year}",
    }


# ------------------------------------------------------- state property tax

def state_tax_rate(state: str | None) -> dict:
    """Approximate effective property tax rate on owner-occupied housing by
    state (percent of value per year). Bundled from data/state_property_tax.json
    (Census ACS-derived estimates); a UI default the investor should verify."""
    path = DATA_DIR / "state_property_tax.json"
    table = json.loads(path.read_text())
    rate = table.get((state or "").upper())
    return {
        "state": state,
        "effective_rate_pct": rate if rate is not None else 1.1,
        "is_estimate": True,
        "note": "Statewide average effective rate — county rates vary; verify locally.",
    }
