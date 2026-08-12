"""Unit tests for the investment math. No network access required."""

import math

import pytest

from app.analysis import DealInputs, analyze, irr, loan_balance, monthly_payment


class TestMortgageMath:
    def test_payment_matches_known_amortization(self):
        # $300,000 at 6.5% for 30 years is $1,896.20/mo by standard tables.
        assert monthly_payment(300_000, 6.5, 30) == pytest.approx(1896.20, abs=0.01)

    def test_zero_interest_is_straight_line(self):
        assert monthly_payment(120_000, 0, 10) == pytest.approx(1000.0)

    def test_no_loan_no_payment(self):
        assert monthly_payment(0, 6.5, 30) == 0.0

    def test_balance_starts_at_principal_and_ends_at_zero(self):
        assert loan_balance(300_000, 6.5, 30, 0) == pytest.approx(300_000)
        assert loan_balance(300_000, 6.5, 30, 360) == pytest.approx(0, abs=0.01)

    def test_balance_never_goes_negative_past_term(self):
        assert loan_balance(300_000, 6.5, 30, 500) == pytest.approx(0, abs=0.01)

    def test_early_payments_are_mostly_interest(self):
        # After one year of a 30-year note, less than 1.5% of principal is repaid.
        paid = 300_000 - loan_balance(300_000, 6.5, 30, 12)
        assert 0 < paid < 300_000 * 0.015


class TestIRR:
    def test_doubling_in_one_year_is_100_pct(self):
        assert irr([-100, 200]) == pytest.approx(1.0, abs=1e-4)

    def test_flat_return_is_zero(self):
        assert irr([-100, 50, 50]) == pytest.approx(0.0, abs=1e-4)

    def test_all_positive_has_no_irr(self):
        assert irr([100, 100]) is None

    def test_known_series(self):
        # 10% annual return on 1000 for 3 years, principal returned at the end.
        assert irr([-1000, 100, 100, 1100]) == pytest.approx(0.10, abs=1e-4)


class TestAnalyze:
    def base(self, **kw):
        args = dict(purchase_price=200_000, monthly_rent=2_000, annual_insurance=1_200)
        args.update(kw)
        return analyze(DealInputs(**args))

    def test_operating_statement_reconciles(self):
        r = self.base()
        y = r["year_one"]
        assert y["effective_gross_income"] == pytest.approx(
            y["gross_rent"] + y["other_income"] - y["vacancy_loss"])
        assert y["noi"] == pytest.approx(y["effective_gross_income"] - y["expenses"]["total"])
        assert y["annual_cash_flow"] == pytest.approx(y["noi"] - y["annual_debt_service"])
        # Both fields are rounded to cents independently, so allow one cent.
        assert y["monthly_cash_flow"] == pytest.approx(y["annual_cash_flow"] / 12, abs=0.01)

    def test_expense_components_sum_to_total(self):
        e = self.base()["year_one"]["expenses"]
        parts = ["property_tax", "insurance", "maintenance", "capex_reserve",
                 "management", "hoa", "utilities", "other"]
        assert sum(e[p] for p in parts) == pytest.approx(e["total"])

    def test_cash_invested_sums_its_parts(self):
        a = self.base(rehab_cost=15_000)["acquisition"]
        assert a["total_cash_invested"] == pytest.approx(
            a["down_payment"] + a["closing_costs"] + a["rehab_cost"])

    def test_all_cash_purchase_has_no_debt_service(self):
        r = self.base(down_payment_pct=100)
        assert r["acquisition"]["loan_amount"] == 0
        assert r["year_one"]["annual_debt_service"] == 0
        assert r["metrics"]["dscr"] is None
        # With no mortgage, cash flow equals NOI.
        assert r["year_one"]["annual_cash_flow"] == pytest.approx(r["year_one"]["noi"])

    def test_cap_rate_is_independent_of_financing(self):
        a = self.base(down_payment_pct=20)["metrics"]["cap_rate_pct"]
        b = self.base(down_payment_pct=50)["metrics"]["cap_rate_pct"]
        assert a == pytest.approx(b)

    def test_higher_rate_reduces_cash_flow(self):
        low = self.base(interest_rate_pct=5.0)["year_one"]["monthly_cash_flow"]
        high = self.base(interest_rate_pct=8.0)["year_one"]["monthly_cash_flow"]
        assert high < low

    def test_projection_length_and_growth(self):
        r = self.base(holding_period_years=7)
        yrs = r["projection"]["years"]
        assert len(yrs) == 7
        assert [y["year"] for y in yrs] == list(range(1, 8))
        # Value appreciates and the loan amortizes, so equity grows every year.
        assert all(b["equity"] > a["equity"] for a, b in zip(yrs, yrs[1:]))

    def test_cumulative_cash_flow_accumulates(self):
        yrs = self.base()["projection"]["years"]
        running = 0.0
        for y in yrs:
            running += y["cash_flow"]
            assert y["cumulative_cash_flow"] == pytest.approx(running, abs=0.02)

    def test_year_one_projection_matches_operating_statement(self):
        r = self.base()
        assert r["projection"]["years"][0]["cash_flow"] == pytest.approx(
            r["year_one"]["annual_cash_flow"], abs=0.02)

    def test_sale_proceeds_net_of_costs_and_loan(self):
        r = self.base(holding_period_years=5, selling_costs_pct=6.0)
        p = r["projection"]
        final = p["years"][-1]
        gross = final["property_value"] * 1.035  # one more year of appreciation
        assert p["net_sale_proceeds"] == pytest.approx(gross * 0.94 - final["loan_balance"], abs=0.02)

    def test_sensitivity_grid_shape_and_centre(self):
        r = self.base()
        assert len(r["sensitivity"]) == 5
        assert all(len(row["cells"]) == 5 for row in r["sensitivity"])
        centre = r["sensitivity"][2]["cells"][2]
        assert r["sensitivity"][2]["rate"] == pytest.approx(6.5)
        assert centre["rent_shift"] == 0
        assert centre["monthly_cash_flow"] == pytest.approx(
            r["year_one"]["monthly_cash_flow"], abs=1.0)

    def test_sensitivity_is_monotonic(self):
        rows = self.base()["sensitivity"]
        # Higher rate -> lower cash flow, at a fixed rent.
        col = [row["cells"][2]["monthly_cash_flow"] for row in rows]
        assert col == sorted(col, reverse=True)
        # Higher rent -> higher cash flow, at a fixed rate.
        row = [c["monthly_cash_flow"] for c in rows[2]["cells"]]
        assert row == sorted(row)

    def test_good_deal_scores_above_bad_deal(self):
        good = self.base(purchase_price=120_000, monthly_rent=1_800)
        bad = self.base(purchase_price=600_000, monthly_rent=1_800)
        assert good["verdict"]["score"] > bad["verdict"]["score"]
        assert good["year_one"]["monthly_cash_flow"] > bad["year_one"]["monthly_cash_flow"]

    def test_score_stays_in_range_across_extremes(self):
        for price, rent in [(50_000, 3_000), (2_000_000, 500), (300_000, 0)]:
            v = self.base(purchase_price=price, monthly_rent=rent)["verdict"]
            assert 0 <= v["score"] <= 100
            assert v["label"] in {"Strong deal", "Worth pursuing", "Marginal", "Weak deal"}

    def test_zero_rent_is_handled(self):
        r = self.base(monthly_rent=0)
        assert r["metrics"]["gross_rent_multiplier"] is None
        assert r["metrics"]["break_even_occupancy_pct"] is None
        assert r["year_one"]["monthly_cash_flow"] < 0

    def test_after_repair_value_drives_taxes_and_projection(self):
        r = self.base(rehab_cost=40_000, after_repair_value=300_000)
        # Property tax is assessed on the ARV, not the purchase price.
        assert r["year_one"]["expenses"]["property_tax"] == pytest.approx(300_000 * 0.011)
        assert r["projection"]["years"][0]["property_value"] == pytest.approx(300_000)

    def test_reasons_are_present_and_readable(self):
        v = self.base()["verdict"]
        assert len(v["reasons"]) >= 4
        assert all(isinstance(x, str) and x.endswith(".") for x in v["reasons"])
        assert not any("$-" in x for x in v["reasons"])  # negatives render as -$X

    def test_irr_reported_for_a_normal_deal(self):
        p = self.base(purchase_price=150_000)["projection"]
        assert p["irr_pct"] is not None
        assert math.isfinite(p["irr_pct"])
