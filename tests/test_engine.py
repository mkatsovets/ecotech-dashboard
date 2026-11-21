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
    # Q2: pool + mgmt != base (151.28 UAH); Sep: owner's pool is 296.89 off the infographic's
    # mgmt, and the reported headline yield 73.08% != 71.3%
    assert ("2026-Q2", "Пул інвесторів + винагорода КК = база") in failed
    assert ("2026-09", "Пул інвесторів + винагорода КК = база") in failed
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
    assert q["Q3 2026"]["pool"] == pytest.approx(8629986.57 + 4116476.72, abs=0.01)
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


def test_gross_income_and_quarter_amounts_reconcile(repo, cfg):
    res = run_all(repo.reports(cfg["id"]), repo.prelims(cfg["id"]), cfg, TODAY)
    m = {x.month: x for x in res["months"]}
    # final months: gross income = reported revenue (before electricity), allocated to months
    assert m[date(2026, 7, 1)].gross + m[date(2026, 8, 1)].gross == pytest.approx(18324039.00, abs=0.01)
    # preliminary / forecast months add an estimated electricity cost on top of the net figure
    assert m[date(2026, 10, 1)].gross > m[date(2026, 10, 1)].net
    assert m[date(2026, 11, 1)].gross > m[date(2026, 11, 1)].net
    for q in res["quarters"]:   # the displayed columns must add up for every quarter
        assert q["gross"] - q["elec"] - q["other_opex"] + q["tax_adj"] == pytest.approx(q["base"], abs=0.01), q["label"]
        # category view: income + returned tax - electricity - personnel - software - profit tax = base
        assert (q["gross"] + q["prior_tax"] - q["elec"] - q["personnel"] - q["software"] - q["profit_tax"]
                == pytest.approx(q["base"], abs=0.01)), q["label"]
        assert q["personnel"] + q["software"] == pytest.approx(q["other_opex"])
    q = {r["label"]: r for r in res["quarters"]}
    assert q["Q2 2026"]["personnel"] == pytest.approx(90062.47) and q["Q2 2026"]["profit_tax"] == pytest.approx(536119.56)
    assert q["Q3 2026"]["prior_tax"] == pytest.approx(536119.56)
    assert q["Q3 2026"]["software"] == pytest.approx(13100.00 + 6666.66)


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


def test_fx_falls_back_to_last_available_day(repo):
    from app.fx import FxError, FxService
    calls = []

    def fetcher(day):
        calls.append(day)
        if day >= date(2026, 11, 21):                 # weekend: nothing published yet
            raise FxError("no data")
        return {"cash_buy": 1, "cash_sell": 2, "card_buy": 3, "card_sell": 45.4546}

    fx = FxService(repo, fetcher=fetcher, today=date(2026, 11, 22))
    assert fx.get(date(2026, 12, 31)) == 45.4546      # future date -> today -> last available
    assert fx.get(date(2026, 11, 21)) == 45.4546      # past weekend day: carried over and cached
    assert repo.get_fx(date(2026, 11, 21)) is not None
    assert repo.get_fx(date(2026, 11, 22)) is None    # today's day is not cached from a fallback


# --- service: reproduce the owner's Excel columns for the reference investor -----------------
def test_service_reference_investor(repo, cfg):
    from app.service import build_dashboard
    d = build_dashboard(repo, FakeFx(), cfg, TODAY)
    pay = {p["key"]: p for p in d["payments"]}
    assert pay["2026-Q2"]["annual_cash"] == pytest.approx(0.2191, abs=2e-3)     # Excel AC37
    assert pay["2026-07-08"]["annual_cash"] == pytest.approx(0.2475, abs=2e-3)  # Excel AC38
    # Excel payouts: 26347.30 + 47301.80 + 28339.52 (September now uses the owner's pool/debt)
    assert d["kpi"]["received_cash_uah"] == pytest.approx(26347.30 + 47301.80 + 28339.52, abs=0.1)
    assert d["kpi"]["xirr_cash"] is not None
    assert d["kpi"]["next_payout"]["label"] == "Q4 2026"


def test_service_cash_and_debt_reconcile(repo, cfg):
    from app.service import build_dashboard
    d = build_dashboard(repo, FakeFx(), cfg, TODAY)
    q = {x["label"]: x for x in d["quarters"]}
    assert q["Q3 2026"]["cash_uah"] + q["Q3 2026"]["debt_uah"] == pytest.approx(q["Q3 2026"]["payout_uah"])
    # estimates split the accrued payout by the assumed debt share (pooled ratio of the last two reports)
    ds = (2933426.57 + 703545.00) / (8629986.57 + 4116476.72)
    assert d["model"]["debt_share"] == pytest.approx(ds)
    q4 = q["Q4 2026"]
    assert q4["split_is_estimate"] and q4["debt_uah"] == pytest.approx(q4["payout_uah"] * ds)
    assert q4["cash_usd"] == pytest.approx(q4["payout_usd"] * (1 - ds))
    assert q4["low"]["cash_usd"] < q4["cash_usd"] < q4["high"]["cash_usd"]
    # locked quarter dollars are the sum of the per-payment dollars (each at its own date's rate)
    pays = {p["key"]: p for p in d["payments"]}
    assert q["Q3 2026"]["cash_usd"] == pytest.approx(pays["2026-07-08"]["cash_usd"] + pays["2026-09"]["cash_usd"])
    assert q["Q3 2026"]["payout_usd"] == pytest.approx(pays["2026-07-08"]["accrued_usd"] + pays["2026-09"]["accrued_usd"])
    assert not q["Q3 2026"]["split_is_estimate"]
    cash = sum(p["cash_uah"] for p in d["payments"])
    assert cash == pytest.approx(q["Q2 2026"]["cash_uah"] + q["Q3 2026"]["cash_uah"])
    assert d["kpi"]["received_cash_uah"] == pytest.approx(cash)
    # average annual yield: cash-only is lower than with the accrued UkrEnergo debt
    assert 0 < d["kpi"]["avg_annual_cash"] < d["kpi"]["avg_annual_accrued"]
    assert any(x["label"] == "Загальна потужність" for x in d["project"]["details"])
    assert all(r["total_income"] - r["opex_total"] - r["profit_tax"] == pytest.approx(r["base"], abs=0.01)
               for r in d["reports"])


def test_service_share_scales_linearly(repo, cfg):
    from app.service import build_dashboard
    a = build_dashboard(repo, FakeFx(), cfg, TODAY)
    b = build_dashboard(repo, FakeFx(), cfg, TODAY, amount=50000)
    assert b["investor"]["share"] == pytest.approx(2 * a["investor"]["share"])
    assert b["kpi"]["received_cash_uah"] == pytest.approx(2 * a["kpi"]["received_cash_uah"])
    # an explicitly edited share overrides the amount-based default
    c = build_dashboard(repo, FakeFx(), cfg, TODAY, amount=25000, share=0.01)
    assert c["investor"]["share"] == 0.01
