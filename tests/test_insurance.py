"""ZIP-level insurance premiums and claims risk."""

import csv
from pathlib import Path

import pytest

from app.insurance import DATA_YEAR, for_zip, national_median

DATA = Path(__file__).resolve().parent.parent / "data"


class TestDataFile:
    def test_bundled_table_is_shaped(self):
        rows = list(csv.DictReader(open(DATA / "insurance_by_zip.csv")))
        assert len(rows) > 20000, "the Treasury release covers ~25k ZIPs"
        head = rows[0]
        for col in ("zip", "premium", "claim_freq_avg", "claim_freq_peak",
                    "loss_ratio_peak", "nonrenewal"):
            assert col in head

    def test_every_premium_is_plausible(self):
        """The tail is real: the dearest ZIPs are Palm Beach, Miami Beach, Sea
        Island and Naples, where an average policy genuinely runs past $20k."""
        for r in csv.DictReader(open(DATA / "insurance_by_zip.csv")):
            p = float(r["premium"])
            assert 400 < p < 60000, f"{r['zip']} premium {p}"

    def test_the_distribution_is_centred_where_it_should_be(self):
        prem = sorted(float(r["premium"])
                      for r in csv.DictReader(open(DATA / "insurance_by_zip.csv")))
        assert 1400 < prem[len(prem) // 2] < 1700, "national median near $1,550"
        assert prem[len(prem) * 99 // 100] < 8000, "99th percentile stays sane"

    def test_zips_are_five_digit_strings(self):
        for r in list(csv.DictReader(open(DATA / "insurance_by_zip.csv")))[:500]:
            assert len(r["zip"]) == 5 and r["zip"].isdigit()


class TestLookup:
    def test_national_median_is_sane(self):
        m = national_median()
        assert 1000 < m < 2500

    def test_known_zips(self):
        lubbock = for_zip("79412")
        cleveland = for_zip("44105")
        assert lubbock["annual_premium"] > cleveland["annual_premium"]
        assert lubbock["data_year"] == DATA_YEAR

    def test_west_texas_reads_as_expensive(self):
        got = for_zip("79412")
        assert got["vs_national"] >= 1.4
        assert any("national median" in f for f in got["findings"])

    def test_hail_exposure_is_called_out(self):
        """79423 peaked near a 47% claim frequency — that must not go unmentioned."""
        got = for_zip("79423")
        assert got["claim_frequency_peak"] > 0.4
        assert any("filed a claim" in f for f in got["findings"])
        assert any("paid out" in f for f in got["findings"]), "a 3.2x loss ratio deserves a line"

    def test_a_quiet_market_gets_no_alarm(self):
        got = for_zip("44107")
        assert got["claim_frequency_peak"] < 0.1
        assert not any("filed a claim" in f for f in got["findings"])

    def test_leading_zero_zips_resolve(self):
        assert for_zip("1001") is not None, "ZIPs are zero-padded on lookup"
        assert for_zip("01001") is not None

    def test_unknown_or_empty_zip_is_none(self):
        assert for_zip("00000") is None
        assert for_zip(None) is None
        assert for_zip("") is None

    def test_vintage_is_disclosed_not_hidden(self):
        got = for_zip("79412")
        assert str(DATA_YEAR) in got["vintage_note"]
        assert "quote" in got["vintage_note"].lower()
        assert "landlord" in got["policy_note"].lower()
        assert got["source"] and got["source_url"]
