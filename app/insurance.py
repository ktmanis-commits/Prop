"""Property insurance cost and claims risk, by ZIP.

A flat national insurance default is wrong almost everywhere, and wrong in both
directions: $1,500 understates West Texas by more than half and overstates
inner-ring Cleveland. This module replaces it with the ZIP's own figure from
the Treasury/NAIC homeowners data call, and surfaces the claims history behind
it — because in hail country the premium is only half the story.

Source: Federal Insurance Office and the NAIC collected ZIP-level data from
insurers covering an annual average of 49.3 million policies, and Treasury
published the aggregate in January 2025. It runs 2018-2022 and is not updated,
so the premium here is a 2022 figure and the hard market since has moved it.
The app says so rather than passing a four-year-old number off as current.
"""

from __future__ import annotations

import csv
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

SOURCE = ("US Treasury Federal Insurance Office and NAIC, homeowners insurance "
          "data call, ZIP-level aggregate")
SOURCE_URL = ("https://home.treasury.gov/system/files/311/"
              "Supporting_Underlying_Metrics_and_Disclaimer_for_Analyses_of_US_"
              "Homeowners_Insurance_Markets_2018-2022.xlsx")
DATA_YEAR = 2022
VINTAGE_NOTE = (f"{DATA_YEAR} premium, the most recent year published. Rates have risen "
                "materially since across most of the country — treat this as a floor and "
                "get a real quote.")
POLICY_NOTE = ("This is the average homeowners premium for an owner-occupied policy. A "
               "landlord policy on the same house is usually written differently and often "
               "costs more.")

_table: dict[str, dict] | None = None


def _load() -> dict[str, dict]:
    global _table
    if _table is None:
        rows: dict[str, dict] = {}
        path = DATA_DIR / "insurance_by_zip.csv"
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                def num(key):
                    try:
                        return float(r[key])
                    except (TypeError, ValueError):
                        return None
                rows[r["zip"].zfill(5)] = {
                    "premium": num("premium"),
                    "claim_freq_avg": num("claim_freq_avg"),
                    "claim_freq_peak": num("claim_freq_peak"),
                    "loss_ratio_peak": num("loss_ratio_peak"),
                    "claim_severity_avg": num("claim_severity_avg"),
                    "nonrenewal": num("nonrenewal"),
                }
        _table = rows
    return _table


def national_median() -> float:
    values = sorted(v["premium"] for v in _load().values() if v["premium"])
    return values[len(values) // 2]


def for_zip(zip_code: str | None) -> dict | None:
    """Premium and claims profile for a ZIP, with the risk read spelled out."""
    if not zip_code:
        return None
    row = _load().get(str(zip_code).strip().zfill(5))
    if not row or not row["premium"]:
        return None

    median = national_median()
    premium = row["premium"]
    ratio = premium / median if median else 1.0

    findings: list[str] = []
    if ratio >= 1.4:
        findings.append(f"At ${premium:,.0f}, insurance here runs {ratio:.1f}x the national "
                        f"median of ${median:,.0f} — a materially larger line item than a "
                        "generic estimate would carry.")
    elif ratio <= 0.75:
        findings.append(f"At ${premium:,.0f}, insurance here runs below the national median "
                        f"of ${median:,.0f}.")

    peak = row["claim_freq_peak"]
    if peak and peak >= 0.20:
        findings.append(f"In the worst of the five years, {peak * 100:.0f}% of policies in "
                        "this ZIP filed a claim. That is storm exposure, and it is what "
                        "drives both premiums and the risk of losing coverage.")
    elif peak and peak >= 0.10:
        findings.append(f"Claim frequency peaked at {peak * 100:.0f}% of policies in a single "
                        "year, so this ZIP sees periodic storm losses.")

    lr = row["loss_ratio_peak"]
    if lr and lr >= 1.5:
        findings.append(f"Insurers paid out {lr:.1f}x premium in the worst year here. "
                        "Sustained losses like that are what precede rate rises and "
                        "tightened underwriting.")

    nonren = row["nonrenewal"]
    if nonren and nonren >= 0.02:
        findings.append(f"{nonren * 100:.1f}% of policies were non-renewed in {DATA_YEAR}.")

    return {
        "zip": str(zip_code).zfill(5),
        "annual_premium": round(premium),
        "national_median": round(median),
        "vs_national": round(ratio, 2),
        "claim_frequency_avg": row["claim_freq_avg"],
        "claim_frequency_peak": peak,
        "loss_ratio_peak": lr,
        "claim_severity_avg": row["claim_severity_avg"],
        "nonrenewal_rate": nonren,
        "data_year": DATA_YEAR,
        "findings": findings,
        "source": SOURCE,
        "source_url": SOURCE_URL,
        "vintage_note": VINTAGE_NOTE,
        "policy_note": POLICY_NOTE,
    }
