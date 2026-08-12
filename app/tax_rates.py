"""Build a property's real property-tax rate from its taxing jurisdictions.

A statewide average is a poor stand-in for the largest operating expense a
landlord carries. Texas rates in particular are the sum of independently
adopted rates — county, city, school district, hospital and water districts —
and the school district alone swings the total by a third within one county.

This module resolves the units that tax a specific parcel and adds their
published rates. Where a county has no jurisdiction table it returns None and
the caller falls back to the statewide average, saying so.
"""

from __future__ import annotations

import json
from pathlib import Path

from .data_sources import DataUnavailable

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

_table: dict | None = None


def table() -> dict:
    global _table
    if _table is None:
        _table = json.loads((DATA_DIR / "tax_jurisdictions.json").read_text())
    return _table


def county_config(county_fips: str) -> dict | None:
    return table().get("counties", {}).get(str(county_fips).zfill(5))


def homestead_rules(state: str | None) -> dict | None:
    return table().get("homestead", {}).get((state or "").upper())


def _school_district(cfg: dict, lat: float, lon: float) -> str | None:
    """Which school district contains this point."""
    layer = cfg.get("school_district_layer")
    if not layer:
        return None
    from .parcels import _query  # local import keeps the dependency one-way
    try:
        rows = _query(layer["url"], {
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": layer["field"],
        })
    except DataUnavailable:
        return None
    if not rows:
        return None
    value = rows[0].get(layer["field"])
    return str(value).strip().upper() if value else None


def effective_rate(county_fips: str, lat: float | None = None, lon: float | None = None,
                   city: str | None = None) -> dict | None:
    """The combined rate for a property, itemised by taxing unit.

    Returns None when the county has no jurisdiction table, so the caller can
    fall back rather than present a fabricated precision.
    """
    cfg = county_config(county_fips)
    if not cfg:
        return None

    units = [dict(u) for u in cfg.get("countywide", [])]
    notes: list[str] = []

    city_key = (city or "").strip().upper()
    city_unit = cfg.get("cities", {}).get(city_key)
    if city_unit:
        units.append({**city_unit, "kind": "city"})
    elif city_key:
        notes.append(f"{city_key.title()} is not an incorporated city in this table; "
                     "no city rate was added.")

    district = None
    if lat is not None and lon is not None:
        district = _school_district(cfg, lat, lon)
    school_unit = cfg.get("school_districts", {}).get(district or "")
    if school_unit:
        units.append({**school_unit, "kind": "school"})
    else:
        notes.append("School district could not be determined, so no school rate is "
                     "included — the true rate will be roughly one point higher.")

    total = sum(float(u["rate"]) for u in units)
    return {
        "effective_rate_pct": round(total, 4),
        "units": [{"name": u["name"], "rate_pct": round(float(u["rate"]), 6),
                   "unit_id": u.get("unit_id")} for u in units],
        "school_district": district,
        "city": city_unit["name"] if city_unit else None,
        "tax_year": cfg.get("tax_year"),
        "source": cfg.get("source"),
        "source_url": cfg.get("source_url"),
        "complete": bool(school_unit),
        "notes": notes,
        "excluded": cfg.get("excluded", []),
    }


def homestead_warning(state: str | None, exemptions: str | None,
                      appraised_value: float | None = None,
                      purchase_price: float | None = None) -> dict | None:
    """Flag that a seller's capped appraisal will not carry to the buyer."""
    rules = homestead_rules(state)
    if not rules or not exemptions:
        return None
    codes = [c.strip().upper() for c in str(exemptions).replace(",", ";").split(";") if c.strip()]
    if not codes:
        return None
    named = [rules.get("codes", {}).get(c, c) for c in codes]
    homesteaded = any(c in ("HS", "OV65", "OA", "DP", "DV") for c in codes)
    if not homesteaded:
        return None

    out = {
        "codes": codes,
        "exemptions": named,
        "cap_pct": rules.get("cap_pct"),
        "headline": rules.get("headline"),
        "detail": rules.get("detail"),
    }
    if appraised_value and purchase_price and purchase_price > appraised_value * 1.02:
        gap = purchase_price - appraised_value
        out["reappraisal_gap"] = round(gap)
        out["reappraisal_gap_pct"] = round(gap / appraised_value * 100, 1)
    return out
