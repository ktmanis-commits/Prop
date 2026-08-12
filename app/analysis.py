"""Core investment math for residential property analysis.

Everything here is a pure function of its inputs so it can be unit-tested
without the web layer or any network access. Monetary values are annual
unless a name says otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DealInputs:
    # Acquisition
    purchase_price: float
    down_payment_pct: float = 20.0          # percent of purchase price
    interest_rate_pct: float = 6.5          # annual note rate, percent
    loan_term_years: int = 30
    closing_costs_pct: float = 3.0          # percent of purchase price
    rehab_cost: float = 0.0
    after_repair_value: float = 0.0         # 0 -> assume purchase price + rehab

    # Income (monthly)
    monthly_rent: float = 0.0
    other_monthly_income: float = 0.0       # parking, laundry, storage...
    vacancy_pct: float = 5.0                # percent of gross rent

    # Operating expenses
    property_tax_rate_pct: float = 1.1      # percent of property value per year
    annual_insurance: float = 1500.0
    maintenance_pct: float = 5.0            # percent of rent
    capex_pct: float = 5.0                  # percent of rent
    management_pct: float = 8.0             # percent of collected rent
    monthly_hoa: float = 0.0
    monthly_utilities: float = 0.0          # owner-paid utilities
    other_monthly_expenses: float = 0.0

    # Projection assumptions
    annual_rent_growth_pct: float = 3.0
    annual_appreciation_pct: float = 3.5
    annual_expense_growth_pct: float = 2.5
    holding_period_years: int = 10
    selling_costs_pct: float = 7.0          # agent commission + closing on sale
    income_tax_note: str = ""               # analysis is pre-income-tax


def monthly_payment(principal: float, annual_rate_pct: float, years: int) -> float:
    """Standard fixed-rate mortgage payment (principal + interest)."""
    if principal <= 0:
        return 0.0
    n = years * 12
    r = annual_rate_pct / 100.0 / 12.0
    if r == 0:
        return principal / n
    return principal * r * (1 + r) ** n / ((1 + r) ** n - 1)


def loan_balance(principal: float, annual_rate_pct: float, years: int, months_elapsed: int) -> float:
    """Remaining balance after `months_elapsed` payments."""
    if principal <= 0:
        return 0.0
    n = years * 12
    m = min(months_elapsed, n)
    r = annual_rate_pct / 100.0 / 12.0
    if r == 0:
        return principal * (1 - m / n)
    pmt = monthly_payment(principal, annual_rate_pct, years)
    return principal * (1 + r) ** m - pmt * (((1 + r) ** m - 1) / r)


def irr(cash_flows: list[float], lo: float = -0.99, hi: float = 10.0) -> float | None:
    """Internal rate of return via bisection. cash_flows[0] is usually negative.

    Returns None when no sign change exists in the bracket (no meaningful IRR).
    """

    def npv(rate: float) -> float:
        return sum(cf / (1 + rate) ** t for t, cf in enumerate(cash_flows))

    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-9:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def _score_component(value: float | None, poor: float, good: float) -> float:
    """Map value linearly to 0..1 between poor and good thresholds."""
    if value is None:
        return 0.0
    if good > poor:
        return max(0.0, min(1.0, (value - poor) / (good - poor)))
    return max(0.0, min(1.0, (poor - value) / (poor - good)))


def analyze(inp: DealInputs) -> dict:
    """Full deal analysis: current-year operating numbers, hold-period
    projection with sale, return metrics, sensitivity grid, and a scored
    verdict with plain-language reasons."""
    price = inp.purchase_price
    down_payment = price * inp.down_payment_pct / 100.0
    loan_amount = price - down_payment
    closing_costs = price * inp.closing_costs_pct / 100.0
    cash_invested = down_payment + closing_costs + inp.rehab_cost
    basis = price + inp.rehab_cost
    start_value = inp.after_repair_value if inp.after_repair_value > 0 else basis

    pi_payment = monthly_payment(loan_amount, inp.interest_rate_pct, inp.loan_term_years)
    annual_debt_service = pi_payment * 12

    # --- Year-1 operating statement ---
    gross_rent = inp.monthly_rent * 12
    other_income = inp.other_monthly_income * 12
    vacancy_loss = gross_rent * inp.vacancy_pct / 100.0
    egi = gross_rent + other_income - vacancy_loss  # effective gross income

    property_tax = start_value * inp.property_tax_rate_pct / 100.0
    maintenance = gross_rent * inp.maintenance_pct / 100.0
    capex = gross_rent * inp.capex_pct / 100.0
    management = (gross_rent - vacancy_loss) * inp.management_pct / 100.0
    fixed = inp.annual_insurance + (inp.monthly_hoa + inp.monthly_utilities + inp.other_monthly_expenses) * 12
    opex = property_tax + maintenance + capex + management + fixed

    noi = egi - opex
    annual_cash_flow = noi - annual_debt_service
    monthly_cash_flow = annual_cash_flow / 12

    cap_rate = noi / start_value * 100 if start_value else None
    coc = annual_cash_flow / cash_invested * 100 if cash_invested > 0 else None
    dscr = noi / annual_debt_service if annual_debt_service > 0 else None
    grm = price / gross_rent if gross_rent > 0 else None
    one_pct = inp.monthly_rent / price * 100 if price else None
    expense_ratio = opex / egi * 100 if egi > 0 else None
    break_even_occ = (opex + annual_debt_service) / (gross_rent + other_income) * 100 if gross_rent + other_income > 0 else None

    # --- Hold-period projection ---
    years = []
    cash_flows = [-cash_invested]
    value = start_value
    rent_y = gross_rent
    other_y = other_income
    tax_y = property_tax
    fixed_y = fixed
    cumulative_cf = 0.0
    sale_proceeds = 0.0

    for year in range(1, inp.holding_period_years + 1):
        if year > 1:
            rent_y *= 1 + inp.annual_rent_growth_pct / 100.0
            other_y *= 1 + inp.annual_rent_growth_pct / 100.0
            fixed_y *= 1 + inp.annual_expense_growth_pct / 100.0
            value *= 1 + inp.annual_appreciation_pct / 100.0
            tax_y = value * inp.property_tax_rate_pct / 100.0
        vac_y = rent_y * inp.vacancy_pct / 100.0
        egi_y = rent_y + other_y - vac_y
        opex_y = (tax_y + fixed_y
                  + rent_y * (inp.maintenance_pct + inp.capex_pct) / 100.0
                  + (rent_y - vac_y) * inp.management_pct / 100.0)
        noi_y = egi_y - opex_y
        debt_y = annual_debt_service if year * 12 <= inp.loan_term_years * 12 else 0.0
        cf_y = noi_y - debt_y
        cumulative_cf += cf_y
        balance = loan_balance(loan_amount, inp.interest_rate_pct, inp.loan_term_years, year * 12)
        equity = value - balance
        years.append({
            "year": year,
            "property_value": round(value, 2),
            "gross_rent": round(rent_y, 2),
            "noi": round(noi_y, 2),
            "cash_flow": round(cf_y, 2),
            "cumulative_cash_flow": round(cumulative_cf, 2),
            "loan_balance": round(balance, 2),
            "equity": round(equity, 2),
        })
        cash_flows.append(cf_y)

    if years:
        final = years[-1]
        end_value = final["property_value"] * (1 + inp.annual_appreciation_pct / 100.0)
        sale_proceeds = end_value * (1 - inp.selling_costs_pct / 100.0) - final["loan_balance"]
        cash_flows[-1] += sale_proceeds

    total_profit = sum(cash_flows)
    deal_irr = irr(cash_flows)
    equity_multiple = (sum(cf for cf in cash_flows[1:])) / cash_invested if cash_invested > 0 else None

    # --- Sensitivity: monthly cash flow across rate and rent shifts ---
    sensitivity = []
    for rate_shift in (-1.0, -0.5, 0.0, 0.5, 1.0):
        row = {"rate": round(inp.interest_rate_pct + rate_shift, 2), "cells": []}
        pmt = monthly_payment(loan_amount, inp.interest_rate_pct + rate_shift, inp.loan_term_years)
        for rent_shift in (-10, -5, 0, 5, 10):
            rent = inp.monthly_rent * (1 + rent_shift / 100.0)
            g = rent * 12
            vac = g * inp.vacancy_pct / 100.0
            mgmt = (g - vac) * inp.management_pct / 100.0
            ox = (property_tax + fixed + g * (inp.maintenance_pct + inp.capex_pct) / 100.0 + mgmt)
            cf = (g + other_income - vac - ox - pmt * 12) / 12
            row["cells"].append({"rent_shift": rent_shift, "monthly_cash_flow": round(cf, 2)})
        sensitivity.append(row)

    # --- Verdict ---
    spread = (cap_rate - inp.interest_rate_pct) if cap_rate is not None else None
    weights = {"coc": 0.35, "dscr": 0.25, "spread": 0.20, "one_pct": 0.20}
    score = 100 * (
        weights["coc"] * _score_component(coc, poor=0.0, good=8.0)
        + weights["dscr"] * _score_component(dscr, poor=1.0, good=1.4)
        + weights["spread"] * _score_component(spread, poor=-2.0, good=1.0)
        + weights["one_pct"] * _score_component(one_pct, poor=0.4, good=1.0)
    )
    if score >= 70:
        verdict = "Strong deal"
    elif score >= 50:
        verdict = "Worth pursuing"
    elif score >= 30:
        verdict = "Marginal"
    else:
        verdict = "Weak deal"

    reasons: list[str] = []
    if monthly_cash_flow >= 200:
        reasons.append(f"Positive cash flow of ${monthly_cash_flow:,.0f}/mo after all expenses and reserves.")
    elif monthly_cash_flow >= 0:
        reasons.append(f"Thin cash flow (${monthly_cash_flow:,.0f}/mo) — little margin for surprises.")
    else:
        reasons.append(f"Negative cash flow of -${abs(monthly_cash_flow):,.0f}/mo — you would feed this property monthly.")
    if dscr is not None:
        if dscr >= 1.25:
            reasons.append(f"DSCR of {dscr:.2f} clears the 1.25 threshold most lenders want.")
        elif dscr >= 1.0:
            reasons.append(f"DSCR of {dscr:.2f} is below the 1.25 most lenders prefer — financing may be harder.")
        else:
            reasons.append(f"DSCR of {dscr:.2f}: rental income does not cover the mortgage.")
    if coc is not None:
        if coc >= 8:
            reasons.append(f"Cash-on-cash return of {coc:.1f}% beats typical 8% investor hurdle.")
        elif coc >= 4:
            reasons.append(f"Cash-on-cash return of {coc:.1f}% is modest; the deal leans on appreciation.")
        else:
            reasons.append(f"Cash-on-cash return of {coc:.1f}% — returns depend almost entirely on appreciation.")
    if spread is not None:
        if spread >= 0.5:
            reasons.append(f"Cap rate exceeds your borrowing rate by {spread:.1f} pts (positive leverage).")
        elif spread >= -0.5:
            reasons.append("Cap rate roughly equals the borrowing rate — leverage neither helps nor hurts much.")
        else:
            reasons.append(f"Cap rate is {abs(spread):.1f} pts below the borrowing rate (negative leverage).")
    if one_pct is not None:
        reasons.append(f"Rent-to-price is {one_pct:.2f}% vs the classic 1% rule of thumb.")

    return {
        "acquisition": {
            "purchase_price": round(price, 2),
            "down_payment": round(down_payment, 2),
            "loan_amount": round(loan_amount, 2),
            "closing_costs": round(closing_costs, 2),
            "rehab_cost": round(inp.rehab_cost, 2),
            "total_cash_invested": round(cash_invested, 2),
            "monthly_pi_payment": round(pi_payment, 2),
        },
        "year_one": {
            "gross_rent": round(gross_rent, 2),
            "other_income": round(other_income, 2),
            "vacancy_loss": round(vacancy_loss, 2),
            "effective_gross_income": round(egi, 2),
            "expenses": {
                "property_tax": round(property_tax, 2),
                "insurance": round(inp.annual_insurance, 2),
                "maintenance": round(maintenance, 2),
                "capex_reserve": round(capex, 2),
                "management": round(management, 2),
                "hoa": round(inp.monthly_hoa * 12, 2),
                "utilities": round(inp.monthly_utilities * 12, 2),
                "other": round(inp.other_monthly_expenses * 12, 2),
                "total": round(opex, 2),
            },
            "noi": round(noi, 2),
            "annual_debt_service": round(annual_debt_service, 2),
            "annual_cash_flow": round(annual_cash_flow, 2),
            "monthly_cash_flow": round(monthly_cash_flow, 2),
        },
        "metrics": {
            "cap_rate_pct": round(cap_rate, 2) if cap_rate is not None else None,
            "cash_on_cash_pct": round(coc, 2) if coc is not None else None,
            "dscr": round(dscr, 2) if dscr is not None else None,
            "gross_rent_multiplier": round(grm, 1) if grm is not None else None,
            "rent_to_price_pct": round(one_pct, 2) if one_pct is not None else None,
            "operating_expense_ratio_pct": round(expense_ratio, 1) if expense_ratio is not None else None,
            "break_even_occupancy_pct": round(break_even_occ, 1) if break_even_occ is not None else None,
            "cap_rate_vs_rate_spread": round(spread, 2) if spread is not None else None,
        },
        "projection": {
            "years": years,
            "sale_year": inp.holding_period_years,
            "net_sale_proceeds": round(sale_proceeds, 2),
            "total_profit": round(total_profit, 2),
            "irr_pct": round(deal_irr * 100, 2) if deal_irr is not None else None,
            "equity_multiple": round(equity_multiple, 2) if equity_multiple is not None else None,
        },
        "sensitivity": sensitivity,
        "verdict": {
            "score": round(score, 0),
            "label": verdict,
            "reasons": reasons,
        },
        "disclaimer": (
            "Pre-income-tax analysis based on your assumptions and public market data. "
            "Not financial advice — verify taxes, insurance, rents and condition locally."
        ),
    }
