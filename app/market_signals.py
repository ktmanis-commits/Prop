"""County-level market fundamentals: jobs, listings, supply, prices, migration.

The deal math in analysis.py answers "do these numbers work?". This module
answers the question underwriting cannot: "will this market still support the
rent in year five?" Everything here reads through FRED's keyless CSV endpoint,
which mirrors BLS, BEA, FHFA, Census and Realtor.com series, so no API key is
required for any of it.

Series are addressed by county FIPS so they resolve for any ZIP in the country.
Small and rural counties are missing some series; every fetch degrades to None
rather than failing the request.
"""

from __future__ import annotations

import json
from pathlib import Path

from .data_sources import (
    TTL_ANNUAL,
    TTL_MONTHLY,
    DataUnavailable,
    county_population,
    fred_series,
    hud_safmr,
    realtor_zip,
    zip_to_county,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# FRED series templates. {f} is the 5-digit county FIPS.
SERIES = {
    # BLS Local Area Unemployment Statistics (monthly, ~3 week lag)
    "unemployed": ("LAUCN{f}0000000004", TTL_MONTHLY),
    "employed": ("LAUCN{f}0000000005", TTL_MONTHLY),
    # Realtor.com residential listings (monthly, ~2 week lag)
    "median_list_price": ("MEDLISPRI{f}", TTL_MONTHLY),
    "days_on_market": ("MEDDAYONMAR{f}", TTL_MONTHLY),
    "active_listings": ("ACTLISCOU{f}", TTL_MONTHLY),
    "price_per_sqft": ("MEDLISPRIPERSQUFEE{f}", TTL_MONTHLY),
    "new_listings": ("NEWLISCOU{f}", TTL_MONTHLY),
    "price_reduced": ("PRIREDCOU{f}", TTL_MONTHLY),
    # FHFA House Price Index (annual, all-transactions)
    "fhfa_hpi": ("ATNHPIUS{f}A", TTL_ANNUAL),
    # BEA per-capita personal income (annual)
    "per_capita_income": ("PCPI{f}", TTL_ANNUAL),
    # Census Building Permits Survey — new privately-owned units authorized
    "building_permits": ("BPPRIV0{f}", TTL_ANNUAL),
}

LABELS = {
    "unemployment_rate": ("Unemployment rate", "BLS Local Area Unemployment Statistics"),
    "employment": ("Employed residents", "BLS Local Area Unemployment Statistics"),
    "median_list_price": ("Median list price", "Realtor.com residential listings"),
    "days_on_market": ("Median days on market", "Realtor.com residential listings"),
    "active_listings": ("Active listings", "Realtor.com residential listings"),
    "price_per_sqft": ("Median list price per sq ft", "Realtor.com residential listings"),
    "new_listings": ("New listings", "Realtor.com residential listings"),
    "price_cut_share": ("Share of listings with a price cut", "Realtor.com residential listings"),
    "fhfa_hpi": ("FHFA house price index", "FHFA all-transactions index"),
    "per_capita_income": ("Per-capita personal income", "BEA regional accounts"),
    "building_permits": ("New housing units permitted", "Census Building Permits Survey"),
}


STATE_ABBR = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
    "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA",
    "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}


def _months_between(a: str, b: str) -> int:
    ya, ma = int(a[:4]), int(a[5:7])
    yb, mb = int(b[:4]), int(b[5:7])
    return (yb - ya) * 12 + (mb - ma)


def _value_n_months_back(dates: list[str], values: list[float], months: int) -> float | None:
    """Observation closest to `months` before the latest, within a 3-month window."""
    if not dates:
        return None
    target = -months
    best, best_gap = None, 99
    for d, v in zip(dates, values):
        gap = abs(_months_between(dates[-1], d) - target)
        if gap < best_gap:
            best, best_gap = v, gap
    return best if best_gap <= 3 else None


def _metric(dates: list[str], values: list[float], months_back: int = 12,
            history_points: int = 60) -> dict:
    """Latest value with its change over `months_back` and a trailing history."""
    prior = _value_n_months_back(dates, values, months_back)
    latest = values[-1]
    change_pct = None
    if prior is not None and prior != 0:
        change_pct = round((latest / prior - 1) * 100, 2)
    return {
        "latest": round(latest, 2),
        "as_of": dates[-1],
        "change_pct": change_pct,
        "change_abs": round(latest - prior, 2) if prior is not None else None,
        "history": {"dates": dates[-history_points:], "values": [round(v, 2) for v in values[-history_points:]]},
    }


def _try(series_key: str, fips: str) -> tuple[list[str], list[float]] | None:
    template, ttl = SERIES[series_key]
    try:
        return fred_series(template.format(f=fips), ttl)
    except DataUnavailable:
        return None


def national_unemployment() -> dict | None:
    try:
        dates, values = fred_series("UNRATE", TTL_MONTHLY)
    except DataUnavailable:
        return None
    return _metric(dates, values)


def county_signals(fips: str, county_name: str = "") -> dict:
    """Assemble every available fundamentals series for one county."""
    out: dict = {"county_fips": fips, "county": county_name, "metrics": {}, "missing": []}
    m = out["metrics"]

    # --- labor market ---
    une = _try("unemployed", fips)
    emp = _try("employed", fips)
    if une and emp:
        # LAUS publishes levels, not the rate, at this granularity: derive it.
        # Both series share a monthly calendar, so align on the shorter tail.
        n = min(len(une[1]), len(emp[1]))
        dates = emp[0][-n:]
        rates = [round(u / (u + e) * 100, 2) for u, e in zip(une[1][-n:], emp[1][-n:]) if (u + e)]
        if rates:
            m["unemployment_rate"] = _metric(dates[-len(rates):], rates)
        m["employment"] = _metric(*emp)
    else:
        out["missing"].append("labor market")

    # --- listings market ---
    for key in ("median_list_price", "days_on_market", "active_listings",
                "price_per_sqft", "new_listings"):
        got = _try(key, fips)
        if got:
            m[key] = _metric(*got)
        else:
            out["missing"].append(key)

    # Price cuts only mean something as a share of what is listed.
    cut, active = _try("price_reduced", fips), _try("active_listings", fips)
    if cut and active:
        n = min(len(cut[1]), len(active[1]))
        dates = active[0][-n:]
        shares = [round(c / a * 100, 2) for c, a in zip(cut[1][-n:], active[1][-n:]) if a]
        if shares:
            m["price_cut_share"] = _metric(dates[-len(shares):], shares)

    # --- prices, incomes, supply pipeline (annual series) ---
    for key, months in (("fhfa_hpi", 12), ("per_capita_income", 12), ("building_permits", 12)):
        got = _try(key, fips)
        if got:
            m[key] = _metric(*got, months_back=months, history_points=30)
        else:
            out["missing"].append(key)

    out["national_unemployment"] = national_unemployment()
    return out


# ----------------------------------------------------------------- migration

def uhaul_growth_index(state: str | None) -> dict | None:
    """U-Haul Growth Index standing for a state.

    U-Haul publishes this only as an annual press release — there is no API or
    data file — so it ships as a dated snapshot in data/migration.json. It is a
    self-selected sample of one-way truck rentals at state resolution, so it is
    carried as directional context beside the measured Census county figure,
    never in place of it.
    """
    path = DATA_DIR / "migration.json"
    if not state or not path.exists():
        return None
    table = json.loads(path.read_text()).get("uhaul_growth_index", {})
    st = state.upper()
    rank = table.get("rankings", {}).get(st)
    if rank is None:
        return None
    detail = table.get("detail", {}).get(st, {})
    prior = detail.get("rank_prior_year")
    return {
        "state": st,
        "rank": rank,
        "of": len(table.get("rankings", {})),
        "rank_prior_year": prior,
        "rank_change": (prior - rank) if prior else None,
        "year": table.get("year"),
        "published": table.get("published"),
        "source": table.get("source"),
        "methodology": table.get("methodology"),
        "note": table.get("note"),
        "top_metros": table.get("top_metros", []),
    }


def county_demographics(fips: str) -> dict | None:
    """Census population growth and measured net domestic migration."""
    try:
        return county_population(fips)
    except DataUnavailable:
        return None


# ----------------------------------------------------------------- scorecard

def _band(value: float | None, poor: float, good: float) -> float | None:
    """Normalize to 0..1 between poor and good (either direction)."""
    if value is None:
        return None
    if good > poor:
        return max(0.0, min(1.0, (value - poor) / (good - poor)))
    return max(0.0, min(1.0, (poor - value) / (poor - good)))


def fundamentals(signals: dict, demographics: dict | None = None,
                 uhaul: dict | None = None, zip_metrics: dict | None = None) -> dict:
    """Score the demand side of the market and explain the score in English.

    Components are weighted by how directly they bear on a landlord's rent roll,
    and the score is computed over whichever components are available so a county
    missing Realtor.com coverage still gets a usable read.
    """
    m = signals.get("metrics", {})
    nat = signals.get("national_unemployment")
    parts: list[tuple[str, float, float]] = []   # (name, weight, 0..1 score)
    findings: list[dict] = []

    # Job growth — the single best predictor of rental demand.
    emp = m.get("employment")
    if emp and emp.get("change_pct") is not None:
        g = emp["change_pct"]
        parts.append(("job growth", 0.30, _band(g, -1.0, 2.0)))
        findings.append({
            "signal": "Job growth",
            "value": f"{g:+.1f}% year over year",
            "verdict": "good" if g >= 1.0 else "neutral" if g >= 0 else "bad",
            "text": (f"Employment grew {g:.1f}% over the past year — a widening tenant pool."
                     if g >= 1.0 else
                     f"Employment is roughly flat ({g:+.1f}% year over year)."
                     if g >= 0 else
                     f"Employment fell {abs(g):.1f}% over the past year, which pressures both rents and vacancy."),
        })

    # Unemployment relative to the nation, not in the abstract.
    un = m.get("unemployment_rate")
    if un:
        rate = un["latest"]
        if nat:
            spread = rate - nat["latest"]
            parts.append(("unemployment", 0.15, _band(spread, 2.0, -1.0)))
            findings.append({
                "signal": "Unemployment",
                "value": f"{rate:.1f}% vs {nat['latest']:.1f}% nationally",
                "verdict": "good" if spread <= -0.5 else "neutral" if spread <= 1.0 else "bad",
                "text": (f"Unemployment of {rate:.1f}% runs {abs(spread):.1f} pts "
                         f"{'below' if spread < 0 else 'above'} the national {nat['latest']:.1f}%."),
            })
        else:
            parts.append(("unemployment", 0.15, _band(rate, 8.0, 3.0)))

    # Listing velocity. ZIP-level readings beat the county average when we have
    # them — a soft county can contain a tight neighborhood and vice versa.
    z = zip_metrics or {}
    scope = "this ZIP" if z.get("days_on_market") is not None else "the county"

    dom_val = z.get("days_on_market")
    dom_chg = z.get("days_on_market_yoy_pct")
    if dom_val is None and m.get("days_on_market"):
        dom_val, dom_chg = m["days_on_market"]["latest"], m["days_on_market"].get("change_pct")
    if dom_val is not None:
        parts.append(("days on market", 0.15, _band(dom_val, 90.0, 30.0)))
        findings.append({
            "signal": "Days on market",
            "value": f"{dom_val:.0f} days",
            "verdict": "good" if dom_val <= 45 else "neutral" if dom_val <= 70 else "bad",
            "text": (f"Homes in {scope} list for a median of {dom_val:.0f} days"
                     + (f", {abs(dom_chg):.0f}% {'slower' if dom_chg > 0 else 'faster'} than a year ago."
                        if dom_chg is not None else ".")
                     + (" A slow market cuts both ways: easier to buy, harder to exit."
                        if dom_val > 70 else "")),
        })

    # Inventory direction — the clearest early sign of softening.
    inv_chg = z.get("active_listings_yoy_pct")
    if inv_chg is None and m.get("active_listings"):
        inv_chg = m["active_listings"].get("change_pct")
    if inv_chg is not None:
        parts.append(("inventory", 0.10, _band(inv_chg, 40.0, -10.0)))
        findings.append({
            "signal": "Inventory",
            "value": f"{inv_chg:+.0f}% year over year",
            "verdict": "good" if inv_chg <= 0 else "neutral" if inv_chg <= 20 else "bad",
            "text": (f"Active listings in {scope} are up {inv_chg:.0f}% year over year — supply is "
                     "building, which favors buyers and caps rent growth." if inv_chg > 20 else
                     f"Active listings in {scope} are {'up' if inv_chg > 0 else 'down'} "
                     f"{abs(inv_chg):.0f}% year over year."),
        })

    # Price cuts — sellers meeting the market or not.
    cut_val = z.get("price_cut_share_pct")
    if cut_val is None and m.get("price_cut_share"):
        cut_val = m["price_cut_share"]["latest"]
    if cut_val is not None:
        parts.append(("price cuts", 0.10, _band(cut_val, 45.0, 15.0)))
        findings.append({
            "signal": "Price cuts",
            "value": f"{cut_val:.0f}% of listings",
            "verdict": "good" if cut_val <= 25 else "neutral" if cut_val <= 38 else "bad",
            "text": (f"{cut_val:.0f}% of active listings in {scope} have cut their price — sellers "
                     "are negotiable, so treat the asking price as a starting point."
                     if cut_val > 30 else
                     f"{cut_val:.0f}% of active listings in {scope} have cut their price."),
        })

    # How much of the market is already spoken for.
    pend = z.get("pending_ratio")
    if pend is not None:
        parts.append(("demand depth", 0.05, _band(pend, 0.1, 0.8)))
        findings.append({
            "signal": "Pending ratio",
            "value": f"{pend:.2f}",
            "verdict": "good" if pend >= 0.5 else "neutral" if pend >= 0.25 else "bad",
            "text": (f"{pend:.2f} homes are under contract for every one still active in this ZIP — "
                     + ("buyers are absorbing supply quickly." if pend >= 0.5
                        else "listings are accumulating faster than they clear.")),
        })

    # Long-run price trend from FHFA, which unlike Zillow is transaction-based.
    hpi = m.get("fhfa_hpi")
    if hpi and hpi.get("change_pct") is not None:
        h = hpi["change_pct"]
        parts.append(("price trend", 0.10, _band(h, -5.0, 6.0)))
        findings.append({
            "signal": "House prices (FHFA)",
            "value": f"{h:+.1f}% year over year",
            "verdict": "good" if h >= 2 else "neutral" if h >= -1 else "bad",
            "text": f"The FHFA all-transactions index for this county moved {h:+.1f}% in the latest year.",
        })

    # Income growth funds rent increases.
    inc = m.get("per_capita_income")
    if inc and inc.get("change_pct") is not None:
        i = inc["change_pct"]
        parts.append(("income growth", 0.10, _band(i, 0.0, 5.0)))
        findings.append({
            "signal": "Income growth",
            "value": f"{i:+.1f}% year over year",
            "verdict": "good" if i >= 3 else "neutral" if i >= 1 else "bad",
            "text": f"Per-capita income moved {i:+.1f}% — rent growth cannot outrun this for long.",
        })

    # Population — the tenant pool itself.
    if demographics and demographics.get("growth_pct_per_year") is not None:
        p = demographics["growth_pct_per_year"]
        parts.append(("population growth", 0.10, _band(p, -1.0, 1.5)))
        findings.append({
            "signal": "Population",
            "value": f"{p:+.1f}%/yr since {demographics['growth_since']}",
            "verdict": "good" if p >= 0.5 else "neutral" if p >= -0.1 else "bad",
            "text": (f"The county's population has grown {p:.1f}%/yr since "
                     f"{demographics['growth_since']} to {demographics['population']:,}."
                     if p >= 0 else
                     f"The county has been losing population at {abs(p):.1f}%/yr since "
                     f"{demographics['growth_since']}, down to {demographics['population']:,}. "
                     "A shrinking tenant pool is the hardest headwind to underwrite around."),
        })

    # Migration — Census county measurement first, U-Haul as regional colour.
    per1k = (demographics or {}).get("net_domestic_migration_per_1k")
    if per1k is not None:
        parts.append(("migration", 0.10, _band(per1k, -8.0, 8.0)))
        bits = [f"Census measures net domestic migration of {per1k:+.1f} per 1,000 county residents"]
        if uhaul:
            bits.append(f"U-Haul ranks {uhaul['state']} #{uhaul['rank']} of {uhaul['of']} "
                        f"for net inbound one-way moves in {uhaul['year']}")
        findings.append({
            "signal": "Migration",
            "value": f"{per1k:+.1f} per 1,000",
            "verdict": "good" if per1k > 1 else "bad" if per1k < -1 else "neutral",
            "text": " · ".join(bits) + ".",
        })
    elif uhaul:
        parts.append(("migration", 0.05, _band(float(uhaul["rank"]), float(uhaul["of"]), 1.0)))
        findings.append({
            "signal": "Migration",
            "value": f"#{uhaul['rank']} of {uhaul['of']}",
            "verdict": "good" if uhaul["rank"] <= uhaul["of"] / 3
                       else "bad" if uhaul["rank"] > 2 * uhaul["of"] / 3 else "neutral",
            "text": (f"U-Haul ranks {uhaul['state']} #{uhaul['rank']} of {uhaul['of']} for net "
                     f"inbound one-way moves in {uhaul['year']}. State-level and a self-selected "
                     "sample — directional only."),
        })

    scored = [(n, w, s) for n, w, s in parts if s is not None]
    if not scored:
        return {"score": None, "label": "No data", "findings": [],
                "note": "No county-level fundamentals series are published for this area."}
    total_w = sum(w for _, w, _ in scored)
    score = round(sum(w * s for _, w, s in scored) / total_w * 100)
    label = ("Strong" if score >= 70 else "Solid" if score >= 55
             else "Mixed" if score >= 40 else "Weak")

    order = {"bad": 0, "neutral": 1, "good": 2}
    findings.sort(key=lambda f: order.get(f["verdict"], 1))
    return {
        "score": score,
        "label": label,
        "findings": findings,
        "components_used": [n for n, _, _ in scored],
        "components_missing": [n for n, _, s in parts if s is None],
    }


def zip_listings(zip_code: str) -> dict | None:
    try:
        return realtor_zip(zip_code)
    except DataUnavailable:
        return None


def rent_benchmarks(zip_code: str) -> dict | None:
    try:
        return hud_safmr(zip_code)
    except DataUnavailable:
        return None


def market_report(zip_code: str, state: str | None = None) -> dict:
    """Everything the fundamentals panel needs for one ZIP."""
    fips, county = zip_to_county(zip_code)
    signals = county_signals(fips, county)
    demographics = county_demographics(fips)
    zip_metrics = zip_listings(zip_code)
    if state is None and demographics:
        state = STATE_ABBR.get(demographics.get("state", ""))
    uhaul = uhaul_growth_index(state)
    return {
        "county_fips": fips,
        "county": county,
        "state": state,
        "signals": signals,
        "zip_listings": zip_metrics,
        "rent_benchmarks": rent_benchmarks(zip_code),
        "demographics": demographics,
        "uhaul": uhaul,
        "fundamentals": fundamentals(signals, demographics, uhaul, zip_metrics),
    }
