# Property Investment Analyzer

Underwrite a US residential rental property in seconds. Enter a ZIP code and the
app pulls live home values, market rents and today's mortgage rate from public
data sources, pre-fills a complete deal, and tells you whether the numbers work —
with the reasoning shown, not just a score.

Built for the investor deciding whether to pursue a specific property.

![The analyzer showing a strong deal in Cleveland, OH](docs/screenshot.png)

## Quick start

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000, type a ZIP code, and click **Pull market data**. The
first market lookup downloads Zillow's research CSVs (~130 MB) and caches them
under `data/cache/` for 24 hours; later lookups are instant.

## What it does

**Pulls the market context for you.** Typical home value and rent for the ZIP,
how both have grown over 1, 5 and 10 years, the local price-to-rent ratio, the
state's average effective property tax rate, and this week's 30-year fixed
mortgage rate. Every one of these becomes an editable input, never a hidden
constant.

**Runs a real pro forma.** A year-one operating statement from gross scheduled
rent down to cash flow, with vacancy, maintenance, CapEx reserves, management,
taxes, insurance and debt service each broken out. Reserves are included by
default because leaving them out is the most common way an amateur analysis
turns a losing property into a winning-looking one.

**Reports the metrics lenders and investors actually use.** Cap rate,
cash-on-cash return, DSCR, gross rent multiplier, rent-to-price ratio, operating
expense ratio, break-even occupancy, IRR over the hold, and equity multiple.

**Projects the hold.** Property value, loan amortization, equity, NOI and
cumulative cash flow year by year, then a sale at the end net of selling costs —
with a table view alongside the chart.

**Stress-tests the deal.** A grid of monthly cash flow across five interest rates
and five rent outcomes, so you can see how much has to go wrong before the
property stops paying for itself.

**Gives a verdict with reasons.** A 0–100 score weighing cash-on-cash return
(35%), DSCR (25%), the spread between cap rate and borrowing rate (20%) and
rent-to-price (20%) — followed by plain-language findings like "Cap rate is 1.2
pts below the borrowing rate (negative leverage)." The reasons matter more than
the score; they tell you *why*.

## Data sources

All public, no API keys required.

| Data | Source | Refresh |
|---|---|---|
| Typical home value (ZHVI) | [Zillow Research](https://www.zillow.com/research/data/) public CSVs, ZIP and metro level | Monthly |
| Typical rent (ZORI) | Zillow Research public CSVs, ZIP level with metro fallback | Monthly |
| 30- and 15-year fixed mortgage rates | Freddie Mac PMMS via [FRED](https://fred.stlouisfed.org/series/MORTGAGE30US) | Weekly |
| Effective property tax rates by state | Census ACS-derived table, bundled in `data/state_property_tax.json` | Static |

Downloads are cached on disk for 24 hours. If a source is unreachable the app
serves the stale cache rather than failing; if there is no cache at all, the API
returns a clear error and the deal inputs still work by hand.

## A note on the assumptions

The suggested appreciation rate is deliberately conservative. Trailing 10-year
growth in some markets exceeds 10%/yr because of the 2020–2022 run-up, and
projecting that forward flatters every deal. The app caps the suggestion at 5%
and floors it at 0, shows you the real 1/5/10-year history next to the field, and
lets you override it.

Analysis is pre-income-tax: no depreciation, mortgage interest deduction, passive
loss treatment or depreciation recapture. Those change after-tax returns
materially and depend on your personal situation — talk to a CPA. Nothing here is
financial advice; verify taxes, insurance, rents and property condition locally
before committing capital.

## API

The web UI is a client of a documented JSON API — interactive docs at `/docs`.

| Endpoint | Purpose |
|---|---|
| `GET /api/prefill?zip=44105&price=95000` | One call: market data, suggested inputs and provenance for a ZIP. `price` is optional and scales the rent estimate. |
| `GET /api/market?zip=44105` or `?metro=Austin` | Home value and rent series with growth rates |
| `GET /api/metros?q=austin` | Metro name lookup |
| `GET /api/rates` | Current and two years of weekly mortgage rates |
| `POST /api/analyze` | Full analysis from a deal payload |

```bash
curl -X POST localhost:8000/api/analyze -H 'Content-Type: application/json' \
  -d '{"purchase_price": 95000, "monthly_rent": 1216, "interest_rate_pct": 6.69}'
```

## Tests

```bash
pytest              # offline: math, API contract, verdict logic
pytest -m live      # additionally hits the live public data sources
```

## Layout

```
app/analysis.py      pure investment math — payments, amortization, IRR, verdict
app/data_sources.py  public data fetchers with disk cache and graceful fallback
app/main.py          FastAPI routes
static/index.html    single-page UI, no build step and no external dependencies
tests/               43 tests, offline by default
```
