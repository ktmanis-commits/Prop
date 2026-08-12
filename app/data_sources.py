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


def _fresh(path: Path) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL_HOURS * 3600


def _download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(".tmp")
    with httpx.stream("GET", url, timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    tmp.replace(dest)


def _fetch_csv(name: str, url: str) -> Path:
    path = _cache_path(name)
    if _fresh(path):
        return path
    try:
        _download(url, path)
    except Exception as exc:
        if path.exists():  # serve stale rather than fail
            return path
        raise DataUnavailable(f"Could not download {name}: {exc}") from exc
    return path


# ---------------------------------------------------------------- FRED rates

def mortgage_rates() -> dict:
    """Latest 30y/15y fixed rates plus two years of weekly history."""
    out: dict = {"source": "FRED (Freddie Mac Primary Mortgage Market Survey)"}
    for key, series in (("thirty_year", "MORTGAGE30US"), ("fifteen_year", "MORTGAGE15US")):
        path = _fetch_csv(f"fred_{series}", FRED_CSV.format(series=series))
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
