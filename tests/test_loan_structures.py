"""Interest-only periods and financed rehab — how the deal is capitalised."""

import pytest

from app.analysis import (
    DealInputs,
    analyze,
    balance_after,
    interest_only_payment,
    loan_payments,
    monthly_payment,
)


def deal(**kw):
    base = dict(purchase_price=200_000, monthly_rent=1_800, annual_insurance=2_000,
                property_tax_rate_pct=1.77)
    base.update(kw)
    return analyze(DealInputs(**base))


class TestInterestOnlyMath:
    def test_io_payment_is_just_the_interest(self):
        assert interest_only_payment(160_000, 6.0) == pytest.approx(800.0)

    def test_io_is_cheaper_than_amortising(self):
        io, amort = loan_payments(160_000, 6.5, 30, io_years=5)
        assert io < amort
        assert io == pytest.approx(interest_only_payment(160_000, 6.5))

    def test_the_amortising_payment_covers_the_shortened_term(self):
        """After 5 interest-only years the loan must clear in the remaining 25."""
        io, amort = loan_payments(160_000, 6.5, 30, io_years=5)
        assert amort == pytest.approx(monthly_payment(160_000, 6.5, 25), abs=0.01)
        assert amort > monthly_payment(160_000, 6.5, 30), "a shorter runway costs more per month"

    def test_no_io_period_is_the_ordinary_loan(self):
        io, amort = loan_payments(160_000, 6.5, 30, io_years=0)
        assert io == 0
        assert amort == pytest.approx(monthly_payment(160_000, 6.5, 30))

    def test_io_years_cannot_exceed_the_term(self):
        io, amort = loan_payments(160_000, 6.5, 10, io_years=30)
        assert io > 0 and amort == 0

    def test_balance_is_untouched_through_the_io_period(self):
        for month in (1, 12, 59, 60):
            assert balance_after(160_000, 6.5, 30, 5, month) == 160_000

    def test_balance_falls_once_amortisation_starts(self):
        assert balance_after(160_000, 6.5, 30, 5, 72) < 160_000

    def test_the_loan_still_clears_by_the_end_of_term(self):
        assert balance_after(160_000, 6.5, 30, 5, 360) == pytest.approx(0, abs=1.0)


class TestInterestOnlyDeal:
    def test_io_lifts_early_cash_flow(self):
        assert deal(interest_only_years=5)["year_one"]["monthly_cash_flow"] > \
               deal()["year_one"]["monthly_cash_flow"]

    def test_io_costs_equity_over_the_hold(self):
        """The flattering part is year one; the bill is equity."""
        plain = deal()["projection"]["years"][-1]["equity"]
        io = deal(interest_only_years=5)["projection"]["years"][-1]["equity"]
        longer = deal(interest_only_years=10)["projection"]["years"][-1]["equity"]
        assert io < plain
        assert longer < io, "ten interest-only years cost more equity than five"

    def test_the_step_up_is_reported_not_buried(self):
        a = deal(interest_only_years=5)["acquisition"]
        assert a["interest_only_years"] == 5
        assert a["payment_step_up"] > 0
        assert a["amortizing_payment"] > a["interest_only_payment"]

    def test_the_verdict_names_the_step_up(self):
        reasons = deal(interest_only_years=5)["verdict"]["reasons"]
        io_reason = next(r for r in reasons if "Interest-only" in r)
        assert "year 6" in io_reason
        assert "owe the same" in io_reason

    def test_payment_steps_up_in_the_projection(self):
        yrs = deal(interest_only_years=3, holding_period_years=6)["projection"]["years"]
        early = yrs[0]["cash_flow"]
        late = yrs[4]["cash_flow"]
        # Rent growth pushes cash flow up, so isolate the debt effect via equity.
        assert yrs[2]["equity"] == pytest.approx(yrs[0]["equity"] +
            (yrs[2]["property_value"] - yrs[0]["property_value"]), abs=1.0), \
            "no principal repaid during the interest-only years"
        assert yrs[4]["loan_balance"] < yrs[2]["loan_balance"]


class TestFinancedRehab:
    def test_financing_lowers_cash_in_and_raises_the_loan(self):
        cash = deal(rehab_cost=40_000)
        fin = deal(rehab_cost=40_000, rehab_financed_pct=100)
        assert fin["acquisition"]["total_cash_invested"] < cash["acquisition"]["total_cash_invested"]
        assert fin["acquisition"]["loan_amount"] > cash["acquisition"]["loan_amount"]
        assert fin["acquisition"]["loan_amount"] - cash["acquisition"]["loan_amount"] == \
            pytest.approx(40_000)

    def test_financing_costs_cash_flow_and_dscr(self):
        cash = deal(rehab_cost=40_000)
        fin = deal(rehab_cost=40_000, rehab_financed_pct=100)
        assert fin["year_one"]["monthly_cash_flow"] < cash["year_one"]["monthly_cash_flow"]
        assert fin["metrics"]["dscr"] < cash["metrics"]["dscr"]

    def test_partial_financing_splits_the_cost(self):
        a = deal(rehab_cost=40_000, rehab_financed_pct=50)["acquisition"]
        assert a["rehab_financed"] == pytest.approx(20_000)
        assert a["rehab_cash"] == pytest.approx(20_000)
        assert a["rehab_financed"] + a["rehab_cash"] == pytest.approx(a["rehab_cost"])

    def test_rehab_still_counts_toward_basis_either_way(self):
        """How it is paid for changes the financing, not the property's value."""
        cash = deal(rehab_cost=40_000)["projection"]["years"][0]["property_value"]
        fin = deal(rehab_cost=40_000, rehab_financed_pct=100)["projection"]["years"][0]["property_value"]
        assert cash == pytest.approx(fin)

    def test_zero_percent_is_the_cash_case(self):
        a = deal(rehab_cost=40_000, rehab_financed_pct=0)["acquisition"]
        assert a["rehab_financed"] == 0
        assert a["rehab_cash"] == pytest.approx(40_000)

    def test_the_verdict_names_financed_rehab(self):
        reasons = deal(rehab_cost=40_000, rehab_financed_pct=100)["verdict"]["reasons"]
        assert any("financed rather than paid in cash" in r for r in reasons)

    def test_no_rehab_says_nothing(self):
        assert not any("rehab" in r.lower() for r in deal()["verdict"]["reasons"])
