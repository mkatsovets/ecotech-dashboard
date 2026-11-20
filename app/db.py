"""SQLite storage. Every table carries project_id so more projects can be added."""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import yaml

from .model import Prelim, Report

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
  project_id TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL,
  period_start TEXT NOT NULL, period_end TEXT NOT NULL, days INTEGER NOT NULL, cycles REAL,
  revenue REAL NOT NULL, prior_tax REAL NOT NULL DEFAULT 0,
  opex_electricity REAL NOT NULL, opex_personnel REAL NOT NULL, opex_software REAL NOT NULL,
  profit_tax REAL NOT NULL DEFAULT 0, base REAL NOT NULL, pool REAL NOT NULL,
  debt_pool REAL NOT NULL DEFAULT 0, mgmt REAL NOT NULL, nbu_rate REAL NOT NULL, pay_date TEXT,
  reported_yield REAL, reported_investor_yield REAL, note TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (project_id, key));
CREATE TABLE IF NOT EXISTS monthly (
  project_id TEXT NOT NULL, month TEXT NOT NULL, days INTEGER NOT NULL, cycles REAL,
  net_uah REAL NOT NULL, status TEXT NOT NULL DEFAULT 'preliminary', note TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (project_id, month));
CREATE TABLE IF NOT EXISTS repayments (
  project_id TEXT NOT NULL, day TEXT NOT NULL, pool_uah REAL NOT NULL, note TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (project_id, day));
CREATE TABLE IF NOT EXISTS fx_rates (
  day TEXT PRIMARY KEY, cash_buy REAL, cash_sell REAL, card_buy REAL, card_sell REAL, fetched_at TEXT);
"""

REPORT_COLS = ["key", "kind", "period_start", "period_end", "days", "cycles", "revenue", "prior_tax",
               "opex_electricity", "opex_personnel", "opex_software", "profit_tax", "base", "pool",
               "debt_pool", "mgmt", "nbu_rate", "pay_date", "reported_yield",
               "reported_investor_yield", "note"]
MONTHLY_COLS = ["month", "days", "cycles", "net_uah", "status", "note"]


def _iso(v):
    return v.isoformat() if isinstance(v, date) else v


class Repo:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # --- reports / monthly -------------------------------------------------
    def upsert_report(self, project_id: str, r: dict) -> None:
        vals = [_iso(r.get(c)) if c != "note" else r.get(c, "") for c in REPORT_COLS]
        cols = ",".join(["project_id"] + REPORT_COLS)
        ph = ",".join("?" * (len(REPORT_COLS) + 1))
        self.conn.execute(f"INSERT OR REPLACE INTO reports ({cols}) VALUES ({ph})", [project_id] + vals)
        self.conn.commit()

    def upsert_monthly(self, project_id: str, m: dict) -> None:
        vals = [_iso(m.get(c)) if c != "note" else m.get(c, "") for c in MONTHLY_COLS]
        cols = ",".join(["project_id"] + MONTHLY_COLS)
        ph = ",".join("?" * (len(MONTHLY_COLS) + 1))
        self.conn.execute(f"INSERT OR REPLACE INTO monthly ({cols}) VALUES ({ph})", [project_id] + vals)
        self.conn.commit()

    def reports(self, project_id: str) -> list[Report]:
        rows = self.conn.execute("SELECT * FROM reports WHERE project_id=? ORDER BY period_start",
                                 (project_id,)).fetchall()
        out = []
        for r in rows:
            d = {c: r[c] for c in REPORT_COLS}
            for k in ("period_start", "period_end", "pay_date"):
                d[k] = date.fromisoformat(d[k]) if d[k] else None
            out.append(Report(**d))
        return out

    def prelims(self, project_id: str) -> list[Prelim]:
        rows = self.conn.execute("SELECT * FROM monthly WHERE project_id=? ORDER BY month",
                                 (project_id,)).fetchall()
        return [Prelim(month=date.fromisoformat(r["month"]), days=r["days"], cycles=r["cycles"],
                       net_uah=r["net_uah"], status=r["status"], note=r["note"]) for r in rows]

    def repayments(self, project_id: str) -> list[tuple[date, float]]:
        rows = self.conn.execute("SELECT day, pool_uah FROM repayments WHERE project_id=? ORDER BY day",
                                 (project_id,)).fetchall()
        return [(date.fromisoformat(r["day"]), r["pool_uah"]) for r in rows]

    # --- fx cache ------------------------------------------------------------
    def get_fx(self, day: date) -> dict[str, float] | None:
        r = self.conn.execute("SELECT * FROM fx_rates WHERE day=?", (day.isoformat(),)).fetchone()
        if r is None:
            return None
        return {k: r[k] for k in ("cash_buy", "cash_sell", "card_buy", "card_sell")}

    def put_fx(self, day: date, rates: dict[str, float], fetched_at: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO fx_rates (day,cash_buy,cash_sell,card_buy,card_sell,fetched_at) "
            "VALUES (?,?,?,?,?,?)",
            (day.isoformat(), rates["cash_buy"], rates["cash_sell"], rates["card_buy"],
             rates["card_sell"], fetched_at))
        self.conn.commit()


def load_config(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def load_seed(repo: Repo, project_id: str, path: str | Path) -> None:
    """Idempotent upsert of the seed file (reports, monthly messages, repayments)."""
    seed = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    for r in seed.get("reports", []):
        repo.upsert_report(project_id, r)
    for m in seed.get("monthly", []):
        repo.upsert_monthly(project_id, m)
    for rp in seed.get("repayments", []) or []:
        repo.conn.execute("INSERT OR REPLACE INTO repayments (project_id, day, pool_uah, note) VALUES (?,?,?,?)",
                          (project_id, _iso(rp["day"]), rp["pool_uah"], rp.get("note", "")))
    repo.conn.commit()
