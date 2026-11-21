# Ecotech Invest dashboard

Public dashboard for investors of the Ecotech Invest energy-storage cooperatives:
all reports in one table, quarterly/monthly dynamics, forecast of the next payout,
and a personal view for any share (default = the reference minimum stake of $25,000).

## Run locally

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m uvicorn app.api:app --port 8765   # http://127.0.0.1:8765/
```

## How it works

- `config/projects/<id>.yaml` — project parameters (capital, profit waterfall tiers, plan,
  reference investor). A new project = a new config + seed file.
- `data/seed/<id>.yaml` — final cooperative reports and monthly preliminary figures.
  The SQLite DB (`data/ecotech.db`, gitignored) is initialised from it on start.
- `app/waterfall.py` — distribution tiers: up to 12% annual yield 100% to investors,
  12-18% 60/40, above 18% 40/60. Yield = base(USD, NBU rate from the report) x 365 / days / capital.
  Reproduces the company's payouts to the kopeck (see `tests/test_engine.py`).
- `app/model.py` — layers per month: final > preliminary (divided by a calibration factor
  learned from history) > forecast (cycles/day x net per cycle, last 3 months, low/base/high).
  A quarter is locked when all its months are covered by final reports.
- `app/fx.py` — Privatbank rate from index.minfin.com.ua (card conversion, "sell"),
  cached in SQLite. Used only for the investor's USD figures; the company's own
  calculations use the NBU rate from the reports.
- `app/returns.py` — XIRR from the investment start date (editable) and run-rate
  annual yield per payout date.

## Admin side (`/admin`)

HTTP Basic auth (`admin` / `ADMIN_PASSWORD`; disabled when the variable is not set).
- final report: form with live consistency checks, or an infographic read by Claude
  (`ANTHROPIC_API_KEY`, model `claude-opus-5-5`, structured output); the result only fills the
  form, the operator confirms;
- monthly chat messages: paste the text, the parser proposes rows (`app/ingest.py`);
- UkrEnergo debt repayments;
- every change goes to the audit log; seed rows edited or deleted in the admin are not
  overwritten or resurrected by the seed file.

Deployment to V/AM: see `docs/DEPLOY_VAM.md`.
