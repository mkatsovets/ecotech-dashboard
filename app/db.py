"""SQLite storage. Every table carries project_id so more projects can be added.

Rows come from two places: the seed file (`source = 'seed'`) and the admin page
(`source = 'admin'`). The seed is re-applied on every start, but it never overwrites a row
edited through the admin page and never resurrects a row the admin deleted (tombstones).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
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
CREATE TABLE IF NOT EXISTS tombstones (
  project_id TEXT NOT NULL, entity TEXT NOT NULL, key TEXT NOT NULL,
  PRIMARY KEY (project_id, entity, key));
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, action TEXT NOT NULL, entity TEXT NOT NULL,
  project_id TEXT NOT NULL, key TEXT NOT NULL, before TEXT, after TEXT);
"""

REPORT_COLS = ["key", "kind", "period_start", "period_end", "days", "cycles", "revenue", "prior_tax",
               "opex_electricity", "opex_personnel", "opex_software", "profit_tax", "base", "pool",
               "debt_pool", "mgmt", "nbu_rate", "pay_date", "reported_yield",
               "reported_investor_yield", "note"]
MONTHLY_COLS = ["month", "days", "cycles", "net_uah", "status", "note"]
# entity -> (table, key column, columns)
ENTITIES = {
    "report": ("reports", "key", REPORT_COLS),
    "monthly": ("monthly", "month", MONTHLY_COLS),
    "repayment": ("repayments", "day", ["day", "pool_uah", "note"]),
}


def _iso(v):
    return v.isoformat() if isinstance(v, date) else v


class Repo:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for table in ("reports", "monthly", "repayments"):    # migration for databases made before `source`
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if "source" not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN source TEXT NOT NULL DEFAULT 'seed'")
        self.conn.commit()

    # --- generic row helpers --------------------------------------------------------------
    def get_row(self, entity: str, project_id: str, key: str) -> dict | None:
        table, kcol, cols = ENTITIES[entity]
        r = self.conn.execute(f"SELECT {','.join(cols)}, source FROM {table} WHERE project_id=? AND {kcol}=?",
                              (project_id, key)).fetchone()
        return dict(r) if r else None

    def list_rows(self, entity: str, project_id: str) -> list[dict]:
        table, kcol, cols = ENTITIES[entity]
        rows = self.conn.execute(f"SELECT {','.join(cols)}, source FROM {table} WHERE project_id=? ORDER BY {kcol}",
                                 (project_id,)).fetchall()
        return [dict(r) for r in rows]

    def put_row(self, entity: str, project_id: str, row: dict, source: str) -> None:
        table, kcol, cols = ENTITIES[entity]
        vals = [_iso(row.get(c)) if c != "note" else row.get(c, "") for c in cols]
        names = ",".join(["project_id"] + cols + ["source"])
        ph = ",".join("?" * (len(cols) + 2))
        self.conn.execute(f"INSERT OR REPLACE INTO {table} ({names}) VALUES ({ph})", [project_id] + vals + [source])
        self.conn.execute("DELETE FROM tombstones WHERE project_id=? AND entity=? AND key=?",
                          (project_id, entity, str(_iso(row[kcol]))))
        self.conn.commit()

    def delete_row(self, entity: str, project_id: str, key: str, tombstone: bool = True) -> bool:
        table, kcol, _ = ENTITIES[entity]
        cur = self.conn.execute(f"DELETE FROM {table} WHERE project_id=? AND {kcol}=?", (project_id, key))
        if tombstone:   # keep the seed from bringing the row back on the next start
            self.conn.execute("INSERT OR IGNORE INTO tombstones (project_id, entity, key) VALUES (?,?,?)",
                              (project_id, entity, key))
        self.conn.commit()
        return cur.rowcount > 0

    def is_tombstoned(self, entity: str, project_id: str, key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM tombstones WHERE project_id=? AND entity=? AND key=?",
                                 (project_id, entity, key)).fetchone() is not None

    def audit(self, action: str, entity: str, project_id: str, key: str, before: dict | None, after: dict | None):
        self.conn.execute(
            "INSERT INTO audit_log (ts, action, entity, project_id, key, before, after) VALUES (?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), action, entity, project_id, key,
             json.dumps(before, ensure_ascii=False, default=str) if before else None,
             json.dumps(after, ensure_ascii=False, default=str) if after else None))
        self.conn.commit()

    def audit_tail(self, project_id: str, limit: int = 50) -> list[dict]:
        rows = self.conn.execute("SELECT ts, action, entity, key, before, after FROM audit_log "
                                 "WHERE project_id=? ORDER BY id DESC LIMIT ?", (project_id, limit)).fetchall()
        return [dict(r) for r in rows]

    # --- model inputs -----------------------------------------------------------------------
    def upsert_report(self, project_id: str, r: dict, source: str = "seed") -> None:
        self.put_row("report", project_id, r, source)

    def upsert_monthly(self, project_id: str, m: dict, source: str = "seed") -> None:
        self.put_row("monthly", project_id, m, source)

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
    """Idempotent seed: updates rows that still belong to the seed, leaves admin-edited rows alone
    and does not bring back rows the admin deleted."""
    seed = yaml.safe_load(Path(path).read_text(encoding="utf-8"))

    def apply(entity: str, row: dict) -> None:
        key = str(_iso(row[ENTITIES[entity][1]]))
        if repo.is_tombstoned(entity, project_id, key):
            return
        existing = repo.get_row(entity, project_id, key)
        if existing is not None and existing["source"] != "seed":
            return
        repo.put_row(entity, project_id, row, "seed")

    for r in seed.get("reports", []):
        apply("report", r)
    for m in seed.get("monthly", []):
        apply("monthly", m)
    for rp in seed.get("repayments", []) or []:
        apply("repayment", rp)
