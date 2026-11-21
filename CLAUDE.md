# Ecotech dashboard — project context for Claude

Public investor dashboard (FastAPI + one static page with Plotly, SQLite). Read `README.md`.
Conversation with the user in Ukrainian; code, comments and commit messages in English; UI in Ukrainian.

## Rules that matter

- Two different FX uses, never mix them: the company's waterfall/yield uses the NBU rate
  stored in each report (`nbu_rate`); the investor's USD figures use the Privatbank card
  "sell" rate on the payout date (`app/fx.py`).
- Source of truth for report numbers is the infographic, with one confirmed exception: for
  September the owner's data win (pool 4116476.72, UkrEnergo debt 703545.00; the infographic
  shows 4116179.83 / 707223.96). The consistency check "pool + mgmt = base" therefore flags
  September (+296.89 UAH); that is expected, do not "fix" the seed to silence it.
- Quarter forecast: a quarter is locked only when all months are final. Nothing past is
  rewritten silently: new reports replace forecast values, history stays.
- Run `pytest` after any change to `app/waterfall.py` or `app/model.py`; the three company
  reports must still be reproduced.
- The seed file is applied on every start, but only to rows that still have `source = 'seed'`:
  rows edited in the admin (`source = 'admin'`) are never overwritten and rows deleted there
  (tombstones) never come back. To correct a seeded value after the admin touched it, edit it
  in the admin, not in the seed file.
- Claude only PROPOSES values from images (`app/ingest.py`); nothing is saved without the
  operator's confirmation in the admin form. Keep it that way: the September Excel typo showed
  how easily a wrong number gets in.
- Python 3.11+; stdlib + FastAPI + PyYAML only. Keep the model pure (no I/O in `model.py`).
