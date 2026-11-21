"""Builds the dashboard payload: project-level data + a personal view for a given share."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date

from .model import check_reports, run_all
from .returns import annualized, xirr
from .waterfall import DAYS_IN_YEAR, tiers_from_config


def _jsonable(v):
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def investor_params(cfg: dict, amount: float | None, share: float | None,
                    start: date | None, valuation: float | None) -> dict:
    """Defaults come from the reference investor; share follows the amount unless set explicitly."""
    ref = cfg["reference_investor"]
    amount = float(amount if amount is not None else ref["invested_usd"])
    per_usd = ref["share"] / ref["invested_usd"]
    return {
        "amount": amount,
        "share": float(share) if share is not None else amount * per_usd,
        "share_per_usd": per_usd,
        "start": start or ref["start_date"],
        "valuation": float(valuation) if valuation is not None else amount,
    }


def build_dashboard(repo, fx, cfg: dict, today: date, amount=None, share=None,
                    start=None, valuation=None) -> dict:
    pid = cfg["id"]
    capital = float(cfg["capital_usd"])
    reports, prelims = repo.reports(pid), repo.prelims(pid)
    repayments = repo.repayments(pid)
    inv = investor_params(cfg, amount, share, start, valuation)
    A, S = inv["amount"], inv["share"]
    warnings: list[str] = []

    model = run_all(reports, prelims, cfg, today)
    rate_today = fx.get(today)

    def usd(uah: float | None, day: date | None) -> float | None:
        if uah is None or day is None:
            return None
        r = fx.get(day)
        if not r:
            warnings.append(f"Немає курсу Приватбанку на {day.isoformat()}")
            return None
        return uah / r

    # --- actual payouts, one row per final report ---------------------------------------
    payments = []
    for r in reports:
        cash_uah = (r.pool - r.debt_pool) * S
        debt_uah = r.debt_pool * S
        rate = fx.get(r.pay_date) if r.pay_date else None
        cash_usd = cash_uah / rate if rate else None
        accr_usd = (cash_uah + debt_uah) / rate if rate else None
        payments.append({
            "key": r.key, "pay_date": r.pay_date, "period_start": r.period_start, "period_end": r.period_end,
            "days": r.days, "cash_uah": cash_uah, "debt_uah": debt_uah, "rate": rate,
            "cash_usd": cash_usd, "accrued_usd": accr_usd,
            "annual_cash": annualized(cash_usd, A, r.days) if cash_usd is not None else None,
            "annual_accrued": annualized(accr_usd, A, r.days) if accr_usd is not None else None,
            "in_return": bool(r.pay_date and r.pay_date >= inv["start"]),
        })
    repaid = []
    for day, pool_uah in repayments:
        uah = pool_uah * S
        repaid.append({"day": day, "uah": uah, "usd": usd(uah, day)})

    # --- quarters with the personal view -------------------------------------------------
    quarters = []
    ds = model["debt_share"]
    pay_by_key = {p["key"]: p for p in payments}
    rep_by_key = {r.key: r for r in reports}
    for q in model["quarters"]:
        row = dict(q)
        row["payout_uah"] = q["pool"] * S                    # accrued: cash + UkrEnergo debt share
        if q["debt"] is not None:                            # locked: actual split from the reports
            row["debt_uah"] = q["debt"] * S
            row["cash_uah"] = (q["pool"] - q["debt"]) * S
            ps = [pay_by_key[k] for k in q.get("report_keys", []) if k in pay_by_key]
            ok = bool(ps) and all(p["accrued_usd"] is not None for p in ps)
            # dollars are summed per payment, each at the Privatbank rate of its own payout date
            row["payout_usd"] = sum(p["accrued_usd"] for p in ps) if ok else None
            row["cash_usd"] = sum(p["cash_usd"] for p in ps) if ok else None
            row["split_is_estimate"] = False
        else:                                                # estimate: split by the assumed debt share
            row["debt_uah"] = row["payout_uah"] * ds
            row["cash_uah"] = row["payout_uah"] * (1 - ds)
            row["payout_usd"] = usd(row["payout_uah"], q["pay_date"])
            row["cash_usd"] = row["payout_usd"] * (1 - ds) if row["payout_usd"] is not None else None
            row["split_is_estimate"] = True
        row["annual"] = annualized(row["payout_usd"], A, q["days"]) if row["payout_usd"] is not None else None
        row["annual_cash"] = annualized(row["cash_usd"], A, q["days"]) if row["cash_usd"] is not None else None
        # project yield recomputed in dollars at the Privatbank rate of the payout date(s)
        # (the company's own figure converts the same base at the NBU rate of the report date)
        if q["debt"] is not None:
            parts = [(rep_by_key[k].base, pay_by_key[k]["rate"]) for k in q.get("report_keys", [])
                     if k in rep_by_key and k in pay_by_key]
            base_usd = sum(b / r for b, r in parts) if parts and all(r for _, r in parts) else None
        else:
            base_usd = usd(q["base"], q["pay_date"])
        row["annual_yield_privat"] = base_usd * DAYS_IN_YEAR / q["days"] / capital if base_usd else None
        # investor's yield without the debt share: the company's yield scaled by cash / accrued
        row["investor_annual_yield_cash"] = (q["investor_annual_yield"] * row["cash_uah"] / row["payout_uah"]
                                             if row["payout_uah"] else 0.0)
        for scen in ("low", "high"):
            if scen in q:
                u = usd(q[scen]["pool"] * S, q["pay_date"])
                row[scen] = {**q[scen], "payout_uah": q[scen]["pool"] * S, "payout_usd": u,
                             "cash_usd": u * (1 - ds) if u is not None else None}
        quarters.append(row)

    # --- KPIs -------------------------------------------------------------------------------
    used = [p for p in payments if p["in_return"]]
    cash_usd = sum(p["cash_usd"] or 0 for p in used) + sum(x["usd"] or 0 for x in repaid if x["day"] >= inv["start"])
    accrued_uah = sum(r.debt_pool for r in reports) * S
    repaid_uah = sum(p * S for _, p in repayments)
    debt_left_uah = accrued_uah - repaid_uah
    debt_left_usd = debt_left_uah / rate_today if rate_today else None

    flows = [(inv["start"], -A)]
    flows += [(p["pay_date"], p["cash_usd"]) for p in used if p["cash_usd"]]
    flows += [(x["day"], x["usd"]) for x in repaid if x["usd"] and x["day"] >= inv["start"]]
    flows_val = flows + [(today, inv["valuation"])]
    flows_debt = flows_val + ([(today, debt_left_usd)] if debt_left_usd else [])

    accr_usd_total = sum(p["accrued_usd"] or 0 for p in used)
    cash_usd_periods = sum(p["cash_usd"] or 0 for p in used)   # payouts only, no repayments
    days_total = sum(p["days"] for p in used)
    next_q = next((q for q in quarters if q["status"] != "locked"), None)

    year_idx = min(10, max(1, int((today - cfg["operation_start"]).days / 365.25) + 1))
    plan = {"year": year_idx,
            "project_yield": cfg["plan"]["project_yield"][year_idx - 1],
            "investor_yield": cfg["plan"]["investor_yield"][year_idx - 1]}

    kpi = {
        "invested_usd": A, "share": S, "valuation_usd": inv["valuation"],
        "received_cash_uah": sum(p["cash_uah"] for p in used) + sum(x["uah"] for x in repaid),
        "received_cash_usd": cash_usd,
        "return_pct": cash_usd / A if A else None,
        "debt_left_uah": debt_left_uah, "debt_left_usd": debt_left_usd,
        "avg_annual_cash": annualized(cash_usd_periods, A, days_total) if days_total else None,
        "avg_annual_accrued": annualized(accr_usd_total, A, days_total) if days_total else None,
        "xirr_cash": xirr(flows_val),
        "xirr_with_debt": xirr(flows_debt),
        "next_payout": None if next_q is None else {
            "label": next_q["label"], "status": next_q["status"], "pay_date": next_q["pay_date"],
            "uah": next_q["payout_uah"], "usd": next_q["payout_usd"], "cash_usd": next_q["cash_usd"],
            "cash_uah": next_q["cash_uah"], "debt_share": ds,
            "low_usd": next_q.get("low", {}).get("payout_usd"), "high_usd": next_q.get("high", {}).get("payout_usd"),
            "low_uah": next_q.get("low", {}).get("payout_uah"), "high_uah": next_q.get("high", {}).get("payout_uah"),
        },
    }

    months = []
    for m in model["months"]:
        months.append({
            "month": m.month, "status": m.status, "days": m.days, "cycles": m.cycles, "net": m.net,
            "gross": m.gross, "elec": m.elec, "gross_low": m.gross_low, "gross_high": m.gross_high,
            "net_per_cycle": (m.net / m.cycles) if m.cycles else None,
            "net_low": m.net_low, "net_high": m.net_high, "raw_prelim": m.raw_prelim,
        })

    out = {
        "project": {"id": pid, "name": cfg["name"], "capital_usd": capital,
                    "tiers": cfg["tiers"], "operation_start": cfg["operation_start"],
                    "details": cfg.get("details", [])},
        "today": today, "investor": inv, "plan": plan, "kpi": kpi,
        "payments": payments, "repayments": repaid, "quarters": quarters, "months": months,
        "reports": [{**asdict(r), "net_ops": r.net_ops, "total_income": r.revenue + r.prior_tax,
                     "opex_total": r.opex_electricity + r.other_opex} for r in reports],
        "prelims": [asdict(p) for p in prelims],
        "checks": check_reports(reports, capital, tiers_from_config(cfg["tiers"])),
        "model": {"calibration": model["calibration"], "debt_share": ds,
                  "params": {"last_n_months": int(cfg.get("forecast", {}).get("last_n_months", 3)),
                             "payout_lag_days": int(cfg.get("forecast", {}).get("payout_lag_days", 25)),
                             "profit_tax_uah": float(cfg.get("forecast", {}).get("profit_tax_uah", 0)),
                             "quarters_ahead": int(cfg.get("forecast", {}).get("quarters_ahead", 1))},
                  "assumptions": {k: asdict(v) for k, v in model["assumptions"].items()}},
        "warnings": sorted(set(warnings)),
    }
    return _jsonable(out)
