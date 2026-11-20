"""Engine tests. The acceptance test: reproduce the company's three final reports."""
from datetime import date
from pathlib import Path

import pytest

from app.fx import parse_privat_html
from app.model import calibration, check_reports, run_all
from app.returns import xirr
from app.waterfall import investor_yield, split_base, tiers_from_config
from tests.conftest import TODAY, FakeFx

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def tiers(cfg):
    return tiers_from_config(cfg["tiers"])


# --- waterfall: examples straight from the presentation ----------------------------------
@pytest.mark.parametrize("project,investor", [
    (0.12, 0.12), (0.17, 0.15), (0.26, 0.188),            # presentation examples
    (0.35, 0.224), (0.33, 0.216), (0.20, 0.164),           # years 1, 2 and 10 of the plan table
])
def test_waterfall_examples(tiers, project, investor):
    assert investor_yield(project, tiers) == pytest.approx(investor, abs=1e-9)


def test_waterfall_nonpositive(tiers):
    assert investor_yield(0, tiers) == 0
    assert split_base(-5, 45, 30, 3e6, tiers).pool_uah == 0


# --- reports: payouts must be reproduced from base + NBU rate ------------------------------
def test_reports_reproduced(repo, cfg, tiers):
    for r in repo.reports(cfg["id"]):
        sp = split_base(r.base, r.nbu_rate, r.days, cfg["capital_usd"], tiers)
        assert sp.pool_uah == pytest.approx(r.pool, rel=5e-4), r.key


def test_checks_flag_the_known_report_issues(repo, cfg, tiers):
    checks = check_reports(repo.reports(cfg["id"]), cfg["capital_usd"], tiers)
    failed = {(c["report"], c["name"]) for c in checks if not c["ok"]}
    # Q2: pool + mgmt != base (151.28 UAH); Sep: reported headline yield 73.08% != 71.3%
    assert ("2026-Q2", "Пул інвесторів + винагорода КК = база") in failed
    assert ("2026-09", "Дохідність проєкту у звіті збігається з розрахунком") in failed
    assert not any(r == "2026-07-08" for r, _ in failed)


# --- model -------------------------------------------------------------------------------
def test_calibration_is_pooled_ratio(repo, cfg):
    k = calibration(repo.reports(cfg["id"]), repo.prelims(cfg["id"]))
    assert k == pytest.approx(0.8918, abs=1e-3)


def test_quarters(repo, cfg):
    res = run_all(repo.reports(cfg["id"]), repo.prelims(cfg["id"]), cfg, TODAY)
    q = {r["label"]: r for r in res["quarters"]}
    assert q["Q2 2026"]["status"] == "locked"
    assert q["Q3 2026"]["status"] == "locked"
    assert q["Q3 2026"]["pool"] == pytest.approx(8629986.57 + 4116179.83, abs=0.01)
    assert q["Q3 2026"]["days"] == 92
    q4 = q["Q4 2026"]
    assert q4["status"] == "estimate"                       # October is in, Nov/Dec are forecast
    assert q4["counts"] == {"final": 0, "reconciled": 0, "preliminary": 1, "forecast": 2}
    assert q4["low"]["pool"] < q4["pool"] < q4["high"]["pool"]
    assert q4["pay_date_estimated"] and q4["pay_date"] == date(2027, 1, 25)
    assert "Q1 2027" in q                                   # one quarter ahead


def test_final_period_is_allocated_to_months(repo, cfg):
    res = run_all(repo.reports(cfg["id"]), repo.prelims(cfg["id"]), cfg, TODAY)
    m = {x.month: x for x in res["months"]}
    jul, aug = m[date(2026, 7, 1)], m[date(2026, 8, 1)]
    assert jul.status == aug.status == "final"
    assert jul.net + aug.net == pytest.approx(18324039.00 - 1864779.09, abs=0.01)
    assert jul.cycles == 60.4 and aug.cycles == 74.5
    assert jul.net / aug.net == pytest.approx(60.4 / 74.5)   # allocated by cycles


def test_forecast_is_replaced_by_actuals(repo, cfg):
    """Adding the final September report must lock Q3 and keep the forecast for Q4 only."""
    reports = repo.reports(cfg["id"])
    only_q2 = [r for r in reports if r.key == "2026-Q2"]
    res = run_all(only_q2, repo.prelims(cfg["id"]), cfg, date(2026, 8, 20))
    q = {r["label"]: r for r in res["quarters"]}
    assert q["Q3 2026"]["status"] == "estimate"             # no final report for Q3 yet
    assert q["Q3 2026"]["counts"] == {"final": 0, "reconciled": 1, "preliminary": 2, "forecast": 0}
    assert q["Q3 2026"]["pool"] > 0


# --- returns -----------------------------------------------------------------------------
def test_xirr_matches_excel():
    flows = [(date(2025, 5, 28), -7170.172084), (date(2025, 5, 29), -9560.229446),
             (date(2025, 5, 30), -8269.59847), (date(2026, 7, 24), 584.9093464),
             (date(2026, 9, 25), 1050.098679), (date(2026, 10, 27), 617.8009923),
             (date(2026, 11, 20), 25000)]
    assert xirr(flows) == pytest.approx(0.06067, abs=2e-4)     # Excel AD65
    assert xirr([(date(2026, 1, 1), 5), (date(2026, 2, 1), 6)]) is None


# --- fx parsing --------------------------------------------------------------------------
def test_parse_privat_fixture():
    rates = parse_privat_html((FIXTURES / "privat_2026-11-20.html").read_text(encoding="utf-8"))
    assert rates == {"cash_buy": 44.9, "cash_sell": 45.55, "card_buy": 45.05, "card_sell": 45.4546}


# --- service: reproduce the owner's Excel columns for the reference investor -----------------
def test_service_reference_investor(repo, cfg):
    from app.service import build_dashboard
    d = build_dashboard(repo, FakeFx(), cfg, TODAY)
    pay = {p["key"]: p for p in d["payments"]}
    assert pay["2026-Q2"]["annual_cash"] == pytest.approx(0.2191, abs=2e-3)     # Excel AC37
    assert pay["2026-07-08"]["annual_cash"] == pytest.approx(0.2475, abs=2e-3)  # Excel AC38
    assert d["kpi"]["received_cash_uah"] == pytest.approx(26347.29 + 47301.78 + 28306.58, abs=1.0)
    assert d["kpi"]["xirr_cash"] is not None
    assert d["kpi"]["next_payout"]["label"] == "Q4 2026"


def test_service_share_scales_linearly(repo, cfg):
    from app.service import build_dashboard
    a = build_dashboard(repo, FakeFx(), cfg, TODAY)
    b = build_dashboard(repo, FakeFx(), cfg, TODAY, amount=50000)
    assert b["investor"]["share"] == pytest.approx(2 * a["investor"]["share"])
    assert b["kpi"]["received_cash_uah"] == pytest.approx(2 * a["kpi"]["received_cash_uah"])
    # an explicitly edited share overrides the amount-based default
    c = build_dashboard(repo, FakeFx(), cfg, TODAY, amount=25000, share=0.01)
    assert c["investor"]["share"] == 0.01
