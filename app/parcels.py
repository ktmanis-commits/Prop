"""County parcel records: the subject property and its recorded sold comps.

There is no national parcel API. Assessor data is published county by county,
mostly as public ArcGIS services with wholly different schemas, so this module
is a small adapter driven by per-county configs in data/county_parcels.json.
Counties without a config degrade to a candidate-layer suggestion rather than
a dead end.

Two things this layer gets right that a naive implementation does not:

* The Census Geocoder returns a point interpolated along a TIGER street
  centerline, which lands in the roadway rather than inside the parcel
  polygon. Point-in-polygon therefore finds nothing; we search a small radius
  and pick the parcel whose address actually matches.
* Twelve states do not make sale prices public record. There, assessed values
  and property characteristics exist but sold comps genuinely do not, and the
  app says so instead of showing an empty table.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from .data_sources import DataUnavailable

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
AGOL_SEARCH = "https://www.arcgis.com/sharing/rest/search"

_registry: dict | None = None


def registry() -> dict:
    global _registry
    if _registry is None:
        _registry = json.loads((DATA_DIR / "county_parcels.json").read_text())
    return _registry


def source_for(county_fips: str) -> dict | None:
    return registry().get("sources", {}).get(str(county_fips).zfill(5))


def is_non_disclosure(state: str | None) -> bool:
    if not state:
        return False
    return state.upper() in registry().get("non_disclosure_states", {}).get("states", [])


# ------------------------------------------------------------- geometry

def to_web_mercator(lon: float, lat: float) -> tuple[float, float]:
    """Lon/lat degrees to EPSG:3857 metres. ArcGIS services frequently decline
    to reproject an input point, so we hand them their own coordinate system."""
    x = lon * 20037508.342789244 / 180.0
    y = math.log(math.tan((90.0 + lat) * math.pi / 360.0)) / (math.pi / 180.0)
    return x, y * 20037508.342789244 / 180.0


def _project(lon: float, lat: float, wkid: int) -> tuple[float, float]:
    if wkid in (102100, 3857, 900913):
        return to_web_mercator(lon, lat)
    return lon, lat  # 4326 and anything else we pass through unchanged


# ----------------------------------------------------------- normalising

_SUFFIX = {
    "AVENUE": "AVE", "STREET": "ST", "ROAD": "RD", "DRIVE": "DR", "BOULEVARD": "BLVD",
    "LANE": "LN", "COURT": "CT", "PLACE": "PL", "TERRACE": "TER", "PARKWAY": "PKWY",
    "CIRCLE": "CIR", "HIGHWAY": "HWY", "TRAIL": "TRL", "SQUARE": "SQ",
}


def normalize_address(value: str | None) -> str:
    if not value:
        return ""
    s = re.sub(r"[.,#]", " ", str(value).upper())
    s = re.sub(r"\s+", " ", s).strip()
    return " ".join(_SUFFIX.get(tok, tok) for tok in s.split())


def _street_key(value: str | None) -> tuple[str, str]:
    """(house number, first street word) — enough to disambiguate neighbours
    without demanding that two databases spell a suffix identically."""
    n = normalize_address(value)
    m = re.match(r"^(\d+)\s+(\S+)", n)
    return (m.group(1), m.group(2)) if m else ("", "")


def _epoch_ms_to_date(value) -> str | None:
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).date().isoformat()
    except (ValueError, OSError, OverflowError):
        return None


# --------------------------------------------------------------- querying

def _query(url: str, params: dict) -> list[dict]:
    params = {"f": "json", "returnGeometry": "false", **params}
    try:
        r = httpx.get(f"{url}/query", params=params, timeout=45,
                      headers={"User-Agent": "property-investment-analyzer/1.0"})
        r.raise_for_status()
        body = r.json()
    except Exception as exc:
        raise DataUnavailable(f"Parcel service unreachable: {exc}") from exc
    if "error" in body:
        raise DataUnavailable(f"Parcel service error: {body['error'].get('message')}")
    return [f.get("attributes", {}) for f in body.get("features", [])]


def _shape(attrs: dict, cfg: dict) -> dict:
    """Translate one county's raw attributes into the app's own vocabulary."""
    f = cfg["fields"]

    def get(key):
        col = f.get(key)
        return attrs.get(col) if col else None

    value = get("market_value")
    basis = cfg.get("value_basis", "market")
    market = None
    if value not in (None, 0):
        market = float(value) if basis == "market" else float(value) / float(basis)

    sale_price = get("last_sale_price")
    sale_price = float(sale_price) if sale_price not in (None, 0) else None
    living = get("living_area")
    living = float(living) if living not in (None, 0) else None

    return {
        "parcel_id": get("parcel_id"),
        "address": (str(get("address")) if get("address") else None),
        "city": get("city"),
        "zip": str(get("zip")) if get("zip") else None,
        "owner": get("owner"),
        "market_value": round(market) if market else None,
        "land_value": get("land_value"),
        "building_value": get("building_value"),
        "living_area": living,
        "rooms": get("rooms"),
        "lot_acres": get("lot_acres"),
        "land_use": get("land_use"),
        "zoning": get("zoning"),
        "buildings": get("buildings"),
        "last_sale_price": sale_price,
        "last_sale_date": _epoch_ms_to_date(get("last_sale_date")),
        "tax_year": get("tax_year"),
        "value_per_sqft": round(market / living, 2) if market and living else None,
    }


def _radius_query(cfg: dict, lon: float, lat: float, radius_m: int,
                  where: str = "1=1", limit: int = 1000) -> list[dict]:
    x, y = _project(lon, lat, cfg.get("wkid", 4326))
    out_fields = ",".join(sorted({v for v in cfg["fields"].values() if v}))
    params = {
        "geometry": f"{x},{y}",
        "geometryType": "esriGeometryPoint",
        "inSR": str(cfg.get("wkid", 4326)),
        "spatialRel": "esriSpatialRelIntersects",
        "distance": str(radius_m),
        "units": "esriSRUnit_Meter",
        "outFields": out_fields,
        "where": where,
        "resultRecordCount": str(limit),
    }
    rows: list[dict] = []
    for url in cfg["urls"]:
        try:
            rows.extend(_shape(a, cfg) for a in _query(url, params))
        except DataUnavailable:
            continue  # one layer of a multi-layer county being down is survivable
    return rows


# ------------------------------------------------------------- public API

def subject_property(county_fips: str, lat: float, lon: float,
                     address: str | None = None) -> dict | None:
    """The parcel at this address. Searches outward until the address matches."""
    cfg = source_for(county_fips)
    if not cfg:
        return None
    want = _street_key(address)
    for radius in (40, 100, 200):
        rows = _radius_query(cfg, lon, lat, radius)
        if not rows:
            continue
        if want != ("", ""):
            exact = [r for r in rows if _street_key(r.get("address")) == want]
            if exact:
                best = max(exact, key=lambda r: r.get("market_value") or 0)
                best["match"] = "address"
                best["source"] = cfg.get("attribution")
                best["county"] = f"{cfg['county']}, {cfg['state']}"
                return best
        if radius == 200:
            # Nothing matched by address; the nearest improved parcel is the
            # best available guess, and we label it as one.
            improved = [r for r in rows if r.get("living_area")]
            if improved:
                best = improved[0]
                best["match"] = "nearest"
                best["source"] = cfg.get("attribution")
                best["county"] = f"{cfg['county']}, {cfg['state']}"
                return best
    return None


def _quantile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated quantile."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _trim_outliers(comps: list[dict], key: str) -> tuple[list[dict], list[dict]]:
    """Split comps into a kept set and the outliers beyond a 1.5-IQR fence.

    Public sale records mix arm's-length transactions with foreclosures,
    family transfers and post-renovation flips, and nothing in the data
    distinguishes them. Trimming the tails is the honest way to get a usable
    central estimate; the discarded rows are returned so they can be shown
    rather than quietly dropped.
    """
    if len(comps) < 5:
        return comps, []
    values = sorted(c[key] for c in comps)
    q1, q3 = _quantile(values, 0.25), _quantile(values, 0.75)
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    kept = [c for c in comps if lo <= c[key] <= hi]
    dropped = [c for c in comps if not (lo <= c[key] <= hi)]
    return (kept, dropped) if len(kept) >= 3 else (comps, [])


def nearby_sales(county_fips: str, lat: float, lon: float, subject: dict | None = None,
                 radius_m: int = 800, years: int = 3, min_price: float = 20000,
                 size_tolerance: float = 0.4, limit: int = 12) -> dict:
    """Recorded sales near the subject, filtered to plausible arm's-length
    transfers of comparable homes."""
    cfg = source_for(county_fips)
    if not cfg:
        return {"available": False, "reason": "no_adapter", "comps": []}
    if is_non_disclosure(cfg.get("state")):
        return {
            "available": False,
            "reason": "non_disclosure_state",
            "comps": [],
            "note": (f"{cfg['state']} is a non-disclosure state: sale prices are not public "
                     "record, so recorded comps cannot be assembled from public data. "
                     "Assessed values and property characteristics are still shown."),
        }

    where = cfg.get("residential_where") or "1=1"
    rows = _radius_query(cfg, lon, lat, radius_m, where=where)
    cutoff = date.today().replace(year=date.today().year - years).isoformat()
    subject_area = (subject or {}).get("living_area")

    comps = []
    for r in rows:
        if not r.get("last_sale_price") or r["last_sale_price"] < min_price:
            continue
        if not r.get("last_sale_date") or r["last_sale_date"] < cutoff:
            continue
        if not r.get("living_area"):
            continue
        if subject and subject.get("parcel_id") and r.get("parcel_id") == subject["parcel_id"]:
            continue
        if subject_area:
            ratio = r["living_area"] / subject_area
            if not (1 - size_tolerance) <= ratio <= (1 + size_tolerance):
                continue
        r = dict(r)
        r["sale_price_per_sqft"] = round(r["last_sale_price"] / r["living_area"], 2)
        comps.append(r)

    comps.sort(key=lambda r: r["last_sale_date"], reverse=True)

    result = {
        "available": bool(comps),
        "radius_m": radius_m,
        "years": years,
        "considered": len(rows),
        "qualifying": len(comps),
        "source": cfg.get("attribution"),
    }
    if not comps:
        result["comps"] = []
        result["reason"] = "no_qualifying_sales"
        result["note"] = (f"No residential sales within {radius_m} m in the past {years} years "
                          "matched the subject's size.")
        return result

    # Statistics run on every qualifying sale; only the most recent are listed.
    kept, dropped = _trim_outliers(comps, "sale_price_per_sqft")
    psf = sorted(c["sale_price_per_sqft"] for c in kept)
    prices = sorted(c["last_sale_price"] for c in kept)
    median_psf = _quantile(psf, 0.5)

    for c in comps:
        c["outlier"] = c not in kept

    result["comps"] = comps[:limit]
    result["median_price_per_sqft"] = round(median_psf, 2)
    result["price_per_sqft_p25"] = round(_quantile(psf, 0.25), 2)
    result["price_per_sqft_p75"] = round(_quantile(psf, 0.75), 2)
    result["median_sale_price"] = round(_quantile(prices, 0.5))
    result["used_for_stats"] = len(kept)
    result["outliers_excluded"] = len(dropped)
    result["dispersion_note"] = (
        f"Sale prices per square foot among these {len(kept)} sales run from "
        f"${psf[0]:,.0f} to ${psf[-1]:,.0f}. Public records cannot distinguish an "
        "arm's-length sale from a foreclosure, family transfer or post-renovation flip, "
        "so treat the median as a starting point and read the individual sales."
        + (f" {len(dropped)} extreme sales were excluded from the median." if dropped else ""))

    if subject_area:
        result["implied_value"] = round(median_psf * subject_area)
        result["implied_value_low"] = round(_quantile(psf, 0.25) * subject_area)
        result["implied_value_high"] = round(_quantile(psf, 0.75) * subject_area)
        result["implied_value_basis"] = (
            f"{len(kept)} recorded sales within {radius_m} m over {years} years, median "
            f"${median_psf:,.0f}/sq ft applied to the subject's {subject_area:,.0f} sq ft")
    return result


def discover_candidates(county: str | None, state: str | None, limit: int = 6) -> list[dict]:
    """Search ArcGIS Online for public parcel layers covering a county.

    Used when no adapter is configured: the app cannot read the data yet, but
    it can point at the layer someone would configure.
    """
    name = (county or "").replace(" County", "").strip()
    if not name:
        return []
    query = f'parcels {name} {state or ""} type:"Feature Service"'.strip()
    try:
        r = httpx.get(AGOL_SEARCH, params={
            "q": query, "f": "json", "num": limit,
            "sortField": "numViews", "sortOrder": "desc",
        }, timeout=30, headers={"User-Agent": "property-investment-analyzer/1.0"})
        r.raise_for_status()
        results = r.json().get("results", [])
    except Exception:
        return []
    return [{"title": x.get("title"), "owner": x.get("owner"), "url": x.get("url")}
            for x in results if x.get("url")]


def parcel_report(county_fips: str, lat: float, lon: float, address: str | None,
                  state: str | None = None, county_name: str | None = None) -> dict:
    """Subject parcel plus comps, or an explanation of why neither is available."""
    cfg = source_for(county_fips)
    if not cfg:
        return {
            "supported": False,
            "county_fips": county_fips,
            "reason": "no_adapter",
            "note": ("No parcel adapter is configured for this county. Assessor records have no "
                     "national API — each county publishes its own service — so this county needs "
                     "a one-time mapping added to data/county_parcels.json."),
            "non_disclosure_state": is_non_disclosure(state),
            "candidate_layers": discover_candidates(county_name, state),
        }
    subject = subject_property(county_fips, lat, lon, address)
    comps = nearby_sales(county_fips, lat, lon, subject)
    return {
        "supported": True,
        "county_fips": county_fips,
        "county": f"{cfg['county']}, {cfg['state']}",
        "source": cfg.get("attribution"),
        "verified": cfg.get("verified"),
        "non_disclosure_state": is_non_disclosure(cfg.get("state")),
        "subject": subject,
        "comps": comps,
    }
