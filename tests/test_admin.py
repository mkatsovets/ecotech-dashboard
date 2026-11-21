"""Admin side: auth, saving, seed interplay, parsers, Claude extraction (with a fake client)."""
import json
import sqlite3
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin import make_router
from app.db import Repo, load_seed
from app.ingest import map_extracted, parse_chat, parse_period_label, report_key
from app.model import run_all
from tests.conftest import ROOT, TODAY

FIXTURES = Path(__file__).parent / "fixtures"
AUTH = ("admin", "s3cret")
HDR = {"X-Admin": "1"}

NEW_REPORT = {
    "period_start": "2026-10-01", "period_end": "2026-10-31", "days": 31, "cycles": 65.8,
    "revenue": 9000000, "prior_tax": 0, "opex_electricity": 1000000, "opex_personnel": 100000,
    "opex_software": 10000, "profit_tax": 0, "base": 7890000, "pool": 4000000, "debt_pool": 500000,
    "mgmt": 3890000, "nbu_rate": 45.3, "pay_date": "2026-11-27", "note": "test",
}


class FakeClaude:
    """Stands in for anthropic.Anthropic: records the request, returns a canned JSON answer."""
    def __init__(self, payload, stop_reason="end_turn"):
        self.calls = []
        self._resp = SimpleNamespace(stop_reason=stop_reason,
                                     content=[SimpleNamespace(type="text", text=json.dumps(payload))])
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return self._resp


SEPT_RAW = {
    "period_label": "ВЕРЕСЕНЬ 2026 РОКУ", "worked_days": 30, "rate_uah_per_usd": 45.2,
    "rate_date_text": "26.09.2026", "revenue": 9157005.61, "prior_tax": None, "opex_total": 1206306.95,
    "opex_electricity": 1054476.07, "opex_personnel": 145164.22, "opex_software": 6666.66,
    "profit_tax": 0.0, "base": 7950698.66, "pool": 4116179.83, "pool_ukrenergo_debt": 707223.96,
    "mgmt": 3834518.83, "project_yield_percent": 73.08, "investor_yield_percent": 37.63,
    "cycles": None, "uncertain_fields": [],
}


@pytest.fixture()
def env(cfg, tmp_path):
    repo = Repo(":memory:")
    load_seed(repo, cfg["id"], ROOT / "data" / "seed" / "eti1.yaml")
    state = {"password": "s3cret", "api_key": "sk-test", "claude": FakeClaude(SEPT_RAW)}
    app = FastAPI()
    app.include_router(make_router(repo, cfg, tmp_path / "uploads", lambda: state["password"],
                                   lambda: state["api_key"], lambda: state["claude"]))
    return SimpleNamespace(repo=repo, client=TestClient(app), state=state, pid=cfg["id"],
                           uploads=tmp_path / "uploads", cfg=cfg)


# --- access control ------------------------------------------------------------------------------
def test_admin_disabled_without_password(env):
    env.state["password"] = None
    assert env.client.get("/api/admin/data", auth=AUTH).status_code == 503


def test_requires_login(env):
    assert env.client.get("/api/admin/data").status_code == 401
    assert env.client.get("/api/admin/data", auth=("admin", "wrong")).status_code == 401
    assert env.client.get("/api/admin/data", auth=AUTH).status_code == 200
    assert env.client.get("/admin", auth=AUTH).status_code == 200


def test_mutations_need_the_csrf_header(env):
    r = env.client.post("/api/admin/report", json=NEW_REPORT, auth=AUTH)           # no X-Admin
    assert r.status_code == 403
    assert env.repo.get_row("report", env.pid, "2026-10") is None


# --- reports ---------------------------------------------------------------------------------------
def test_save_report_audit_and_model(env):
    r = env.client.post("/api/admin/report", json=NEW_REPORT, auth=AUTH, headers=HDR)
    assert r.status_code == 200, r.text
    assert r.json()["key"] == "2026-10" and r.json()["created"] is True
    row = env.repo.get_row("report", env.pid, "2026-10")
    assert row["source"] == "admin" and row["kind"] == "final"
    assert env.repo.audit_tail(env.pid)[0]["action"] == "create"
    # October is now a final month: Q4 moves from "forecast of everything" to a mixed estimate
    res = run_all(env.repo.reports(env.pid), env.repo.prelims(env.pid), env.cfg, TODAY)
    q4 = {q["label"]: q for q in res["quarters"]}["Q4 2026"]
    assert q4["counts"]["final"] == 1 and q4["counts"]["forecast"] == 2
    # saving again updates and logs an update
    NEW = {**NEW_REPORT, "note": "changed"}
    assert env.client.post("/api/admin/report", json=NEW, auth=AUTH, headers=HDR).json()["created"] is False
    assert env.repo.audit_tail(env.pid)[0]["action"] == "update"


def test_overlapping_period_is_rejected(env):
    clash = {**NEW_REPORT, "period_start": "2026-09-15", "period_end": "2026-10-15"}
    r = env.client.post("/api/admin/report", json=clash, auth=AUTH, headers=HDR)
    assert r.status_code == 409 and "2026-09" in r.json()["detail"]


def test_validation(env):
    bad = {**NEW_REPORT, "period_end": "2026-09-30"}
    assert env.client.post("/api/admin/report", json=bad, auth=AUTH, headers=HDR).status_code == 422
    bad = {**NEW_REPORT, "debt_pool": 9e9}
    assert env.client.post("/api/admin/report", json=bad, auth=AUTH, headers=HDR).status_code == 422
    bad = {**NEW_REPORT, "key": "../etc"}
    assert env.client.post("/api/admin/report", json=bad, auth=AUTH, headers=HDR).status_code == 422


def test_check_endpoint_flags_problems_without_saving(env):
    off = {**NEW_REPORT, "mgmt": 3000000}                                  # pool + mgmt != base
    r = env.client.post("/api/admin/report/check", json=off, auth=AUTH, headers=HDR).json()
    assert r["key"] == "2026-10"
    assert any(not c["ok"] and "Пул" in c["name"] for c in r["checks"])
    assert env.repo.get_row("report", env.pid, "2026-10") is None


# --- seed vs admin edits ------------------------------------------------------------------------------
def test_seed_does_not_overwrite_admin_edits_or_resurrect_deleted_rows(env):
    seed = ROOT / "data" / "seed" / "eti1.yaml"
    sept = env.repo.get_row("report", env.pid, "2026-09")
    edited = {k: sept[k] for k in sept if k != "source"}
    edited.update(note="edited by admin", pool=4116476.72)
    r = env.client.post("/api/admin/report", json=edited, auth=AUTH, headers=HDR)
    assert r.status_code == 200, r.text
    load_seed(env.repo, env.pid, seed)                                      # simulates a restart
    assert env.repo.get_row("report", env.pid, "2026-09")["note"] == "edited by admin"

    assert env.client.delete("/api/admin/report/2026-Q2", auth=AUTH, headers=HDR).status_code == 200
    load_seed(env.repo, env.pid, seed)
    assert env.repo.get_row("report", env.pid, "2026-Q2") is None           # tombstone holds
    assert env.client.delete("/api/admin/report/2026-Q2", auth=AUTH, headers=HDR).status_code == 404

    q2 = {**NEW_REPORT, "key": "2026-Q2", "period_start": "2026-05-23", "period_end": "2026-06-30", "days": 39}
    assert env.client.post("/api/admin/report", json=q2, auth=AUTH, headers=HDR).status_code == 200
    assert env.repo.is_tombstoned("report", env.pid, "2026-Q2") is False     # re-adding clears it


def test_old_database_gets_the_source_column(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE reports (project_id TEXT, key TEXT, kind TEXT, period_start TEXT, period_end TEXT,"
                " days INTEGER, cycles REAL, revenue REAL, prior_tax REAL, opex_electricity REAL, opex_personnel REAL,"
                " opex_software REAL, profit_tax REAL, base REAL, pool REAL, debt_pool REAL, mgmt REAL, nbu_rate REAL,"
                " pay_date TEXT, reported_yield REAL, reported_investor_yield REAL, note TEXT, PRIMARY KEY (project_id, key))")
    con.commit(); con.close()
    repo = Repo(path)
    assert "source" in {r["name"] for r in repo.conn.execute("PRAGMA table_info(reports)")}


# --- monthly and repayments -----------------------------------------------------------------------------
def test_monthly_save_normalises_month_and_feeds_the_forecast(env):
    nov = {"month": "2026-11-15", "days": 30, "cycles": 66.0, "net_uah": 6800000, "status": "preliminary"}
    r = env.client.post("/api/admin/monthly", json=[nov], auth=AUTH, headers=HDR)
    assert r.json()["saved"] == ["2026-11-01"]
    res = run_all(env.repo.reports(env.pid), env.repo.prelims(env.pid), env.cfg, TODAY)
    q4 = {q["label"]: q for q in res["quarters"]}["Q4 2026"]
    assert q4["counts"] == {"final": 0, "reconciled": 0, "preliminary": 2, "forecast": 1}
    assert env.client.delete("/api/admin/monthly/2026-11-01", auth=AUTH, headers=HDR).status_code == 200


def test_repayment_reduces_the_debt(env):
    from app.fx import FxService
    from app.service import build_dashboard
    from tests.conftest import FakeFx
    before = build_dashboard(env.repo, FakeFx(), env.cfg, TODAY)["kpi"]["debt_left_uah"]
    r = env.client.post("/api/admin/repayment", json={"day": "2026-11-10", "pool_uah": 1000000}, auth=AUTH, headers=HDR)
    assert r.status_code == 200
    after = build_dashboard(env.repo, FakeFx(), env.cfg, TODAY)["kpi"]["debt_left_uah"]
    assert after == pytest.approx(before - 1000000 * env.cfg["reference_investor"]["share"])


# --- parsers -------------------------------------------------------------------------------------------------
def test_parse_chat_with_the_four_real_messages():
    rows, problems = parse_chat((FIXTURES / "chat.txt").read_text(encoding="utf-8"))
    assert problems == []
    got = [(r["month"], r["days"], r["cycles"], r["net_uah"], r["status"]) for r in rows]
    assert got == [("2026-07-01", 31, 60.4, 6456618.0, "reconciled"),
                   ("2026-08-01", 31, 74.5, 8054348.0, "preliminary"),
                   ("2026-09-01", 30, 63.8, 7393943.0, "preliminary"),
                   ("2026-10-01", 31, 65.8, 6491289.0, "preliminary")]


def test_parse_chat_reports_problems():
    rows, problems = parse_chat("Вітаю, інвестори!\nРезультати за щось.")
    assert rows == [] and problems
    rows, problems = parse_chat("Вітаю, інвестори!\nза листопад 2026 року\nкількість робочих днів — 30")
    assert rows == [] and "суму" in problems[0]


def test_parse_chat_endpoint(env):
    text = (FIXTURES / "chat.txt").read_text(encoding="utf-8")
    r = env.client.post("/api/admin/parse-chat", json={"text": text}, auth=AUTH, headers=HDR).json()
    assert len(r["rows"]) == 4 and r["problems"] == []


@pytest.mark.parametrize("label,span,key", [
    ("ВЕРЕСЕНЬ 2026 РОКУ", (date(2026, 9, 1), date(2026, 9, 30)), "2026-09"),
    ("ЛИПЕНЬ – СЕРПЕНЬ 2026 РОКУ", (date(2026, 7, 1), date(2026, 8, 31)), "2026-07-08"),
    ("II КВАРТАЛ 2026 РОКУ", (date(2026, 4, 1), date(2026, 6, 30)), "2026-Q2"),
])
def test_period_label_and_key(label, span, key):
    assert parse_period_label(label) == span
    assert report_key(*span) == key
    assert parse_period_label("без року") is None


# --- Claude extraction (fake client) ---------------------------------------------------------------------------
def test_map_extracted_flags_cost_mismatch():
    ok = map_extracted(SEPT_RAW)
    assert ok["uncertain"] == []
    f = ok["fields"]
    assert (f["key"], f["period_start"], f["period_end"], f["days"]) == ("2026-09", "2026-09-01", "2026-09-30", 30)
    assert f["reported_yield"] == pytest.approx(0.7308) and f["prior_tax"] == 0 and f["pay_date"] is None
    bad = map_extracted({**SEPT_RAW, "opex_personnel": 145164.22 + 5000})
    assert any("сума не збігається" in u for u in bad["uncertain"])


def test_parse_image_endpoint(env):
    import base64
    png = base64.b64encode(b"\x89PNG fake").decode()
    r = env.client.post("/api/admin/parse-image", auth=AUTH, headers=HDR,
                        json={"filename": "Звіт вересень.png", "media_type": "image/png", "data_base64": png})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fields"]["pool"] == 4116179.83 and body["fields"]["key"] == "2026-09"
    call = env.state["claude"].calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["effort"] == "medium"
    assert call["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in call["betas"]
    block = call["messages"][0]["content"][0]
    assert block["type"] == "image" and block["source"]["data"] == png
    assert (env.uploads / body["saved_as"]).read_bytes() == b"\x89PNG fake"     # original kept for audit
    assert env.repo.get_row("report", env.pid, "2026-09")["pool"] == 4116476.72  # nothing saved by parsing


def test_parse_image_guards(env):
    import base64
    png = base64.b64encode(b"x").decode()
    post = lambda **kw: env.client.post("/api/admin/parse-image", auth=AUTH, headers=HDR,
                                        json={"filename": "a.png", "media_type": "image/png", "data_base64": png, **kw})
    assert post(media_type="application/pdf").status_code == 422
    assert post(data_base64="***not base64***").status_code == 400
    env.state["claude"] = FakeClaude(SEPT_RAW, stop_reason="refusal")
    assert post().status_code == 422
    env.state["api_key"] = None
    assert post().status_code == 503
