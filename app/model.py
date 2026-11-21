"""Core model: final reports + monthly preliminary data -> months -> quarters + forecast.

Layers per month, by priority:
  final        covered by a final cooperative report (period reports are allocated to months)
  preliminary  monthly chat message; "net after electricity" divided by a calibration factor
               k = sum(preliminary) / sum(final) over periods where both exist
  forecast     cycles/day x net per cycle from the last N months

A quarter is "locked" when every month in it is final; otherwise it is an estimate
(the waterfall is applied to the quarter as a whole) and is replaced by the real
numbers when the company's final reports arrive. Pure functions, no I/O.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean

from .waterfall import DAYS_IN_YEAR, split_base, tiers_from_config


@dataclass
class Report:
    key: str
    kind: str
    period_start: date
    period_end: date
    days: int
    cycles: float | None
    revenue: float
    prior_tax: float
    opex_electricity: float
    opex_personnel: float
    opex_software: float
    profit_tax: float
    base: float
    pool: float
    debt_pool: float
    mgmt: float
    nbu_rate: float
    pay_date: date | None
    reported_yield: float | None
    reported_investor_yield: float | None
    note: str = ""

    @property
    def net_ops(self) -> float:      # revenue after electricity purchases
        return self.revenue - self.opex_electricity

    @property
    def other_opex(self) -> float:
        return self.opex_personnel + self.opex_software


@dataclass
class Prelim:
    month: date
    days: int
    cycles: float | None
    net_uah: float                   # revenue minus electricity purchases, as published in the chat
    status: str = "preliminary"      # preliminary | reconciled
    note: str = ""


@dataclass
class MonthRow:
    month: date
    status: str                      # final | reconciled | preliminary | forecast
    days: int
    cycles: float | None
    net: float                       # revenue after electricity (calibrated for preliminary)
    other_opex: float
    adj: float = 0.0                 # prior-period tax returned minus profit tax
    elec: float = 0.0                # electricity purchase cost (actual for final, estimated otherwise)
    pool: float | None = None
    debt: float | None = None
    mgmt: float | None = None
    fx: float | None = None
    pay_date: date | None = None
    report_key: str | None = None
    raw_prelim: float | None = None
    net_low: float | None = None
    net_high: float | None = None
    gross_low: float | None = None
    gross_high: float | None = None

    @property
    def base(self) -> float:
        return self.net - self.other_opex + self.adj

    @property
    def gross(self) -> float:        # total income before electricity purchases
        return self.net + self.elec

    @property
    def is_final(self) -> bool:
        return self.status == "final"


@dataclass
class Assumptions:
    cycles_per_day: float
    net_per_cycle: float
    other_opex_per_day: float
    fx: float
    elec_per_cycle: float = 0.0


# --- helpers ---------------------------------------------------------------------------

def month_overlaps(start: date, end: date) -> list[tuple[date, int]]:
    """[(first day of month, days of [start, end] inside that month)]"""
    out, d = [], date(start.year, start.month, 1)
    while d <= end:
        last = date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
        s, e = max(start, d), min(end, last)
        out.append((d, (e - s).days + 1))
        d = last + timedelta(days=1)
    return out


def quarter_key(d: date) -> tuple[int, int]:
    return d.year, (d.month - 1) // 3 + 1


def quarter_end(year: int, q: int) -> date:
    m = q * 3
    return date(year, m, calendar.monthrange(year, m)[1])


def other_opex_per_day(reports: list[Report]) -> float:
    days = sum(r.days for r in reports)
    return sum(r.other_opex for r in reports) / days if days else 0.0


def electricity_per_cycle(reports: list[Report]) -> float:
    """Electricity purchase cost per cycle, from final reports that state the cycles."""
    cycles = sum(r.cycles for r in reports if r.cycles)
    cost = sum(r.opex_electricity for r in reports if r.cycles)
    return cost / cycles if cycles else 0.0


def calibration(reports: list[Report], prelims: list[Prelim]) -> float:
    """k = preliminary net / final net, pooled over periods where both exist."""
    pm = {p.month: p for p in prelims}
    sp = sf = 0.0
    for r in reports:
        ms = [m for m, _ in month_overlaps(r.period_start, r.period_end)]
        if ms and all(m in pm for m in ms):
            sp += sum(pm[m].net_uah for m in ms)
            sf += r.net_ops
    return sp / sf if sf > 0 and sp > 0 else 1.0


# --- months ----------------------------------------------------------------------------

def build_months(reports: list[Report], prelims: list[Prelim], k: float,
                 elec_per_cycle: float = 0.0) -> list[MonthRow]:
    pm = {p.month: p for p in prelims}
    rows: dict[date, MonthRow] = {}
    for r in reports:
        ov = month_overlaps(r.period_start, r.period_end)
        cyc = [pm[m].cycles if m in pm else None for m, _ in ov]
        use_cycles = all(c is not None for c in cyc) and sum(cyc) > 0
        weights = [float(c) for c in cyc] if use_cycles else [float(d) for _, d in ov]
        total_w, total_d = sum(weights), sum(d for _, d in ov)
        for (m, d), w, c in zip(ov, weights, cyc):
            f = w / total_w
            cycles = c if c is not None else (r.cycles * d / total_d if r.cycles else None)
            rows[m] = MonthRow(
                month=m, status="final", days=d, cycles=cycles,
                net=r.net_ops * f, other_opex=r.other_opex * f, elec=r.opex_electricity * f,
                adj=(r.prior_tax - r.profit_tax) * f,
                pool=r.pool * f, debt=r.debt_pool * f, mgmt=r.mgmt * f,
                fx=r.nbu_rate, pay_date=r.pay_date, report_key=r.key,
                raw_prelim=pm[m].net_uah if m in pm else None)
    opd = other_opex_per_day(reports)
    for p in prelims:
        if p.month not in rows:
            rows[p.month] = MonthRow(
                month=p.month, status=p.status, days=p.days, cycles=p.cycles,
                net=p.net_uah / k, other_opex=opd * p.days, raw_prelim=p.net_uah,
                elec=elec_per_cycle * (p.cycles or 0.0))
    return [rows[m] for m in sorted(rows)]


def derive_assumptions(months: list[MonthRow], reports: list[Report], fc: dict) -> dict[str, Assumptions]:
    """base / low / high assumptions from the last N months that have cycles and revenue."""
    hist = [m for m in months if m.cycles and m.cycles > 0 and m.days > 0 and m.net > 0]
    hist = hist[-int(fc.get("last_n_months", 3)):]
    if not hist or not reports:
        return {}
    cpd = [m.cycles / m.days for m in hist]
    npc = [m.net / m.cycles for m in hist]
    latest = max(reports, key=lambda r: r.period_end)
    fx = fc.get("fx_uah_per_usd") or latest.nbu_rate
    opd = other_opex_per_day(reports)
    epc = electricity_per_cycle(reports)
    return {
        "base": Assumptions(mean(cpd), mean(npc), opd, fx, epc),
        "low": Assumptions(min(cpd), min(npc), opd, fx, epc),
        "high": Assumptions(max(cpd), max(npc), opd, fx, epc),
    }


def extend_with_forecast(months: list[MonthRow], a: Assumptions, through: date) -> list[MonthRow]:
    out = list(months)
    d = out[-1].month if out else None
    if d is None:
        return out
    while True:
        d = date(d.year + (d.month // 12), d.month % 12 + 1, 1)
        if d > through:
            break
        days = calendar.monthrange(d.year, d.month)[1]
        cycles = a.cycles_per_day * days
        out.append(MonthRow(month=d, status="forecast", days=days, cycles=cycles,
                            net=cycles * a.net_per_cycle, other_opex=a.other_opex_per_day * days,
                            elec=cycles * a.elec_per_cycle))
    return out


# --- quarters --------------------------------------------------------------------------

def build_quarters(months: list[MonthRow], capital: float, tiers, fx_fallback: float,
                   profit_tax: float, lag_days: int) -> list[dict]:
    groups: dict[tuple[int, int], list[MonthRow]] = {}
    for m in months:
        groups.setdefault(quarter_key(m.month), []).append(m)
    out = []
    for (y, q), ms in sorted(groups.items()):
        days = sum(m.days for m in ms)
        counts = {s: sum(1 for m in ms if m.status == s) for s in ("final", "reconciled", "preliminary", "forecast")}
        cycles = sum(m.cycles for m in ms) if all(m.cycles is not None for m in ms) else None
        net = sum(m.net for m in ms)
        gross = sum(m.gross for m in ms)
        other = sum(m.other_opex for m in ms)
        locked = all(m.is_final for m in ms)
        row = {"label": f"Q{q} {y}", "year": y, "quarter": q, "days": days, "cycles": cycles,
               "months": [m.month.isoformat() for m in ms], "counts": counts, "net": net,
               "gross": gross, "elec": gross - net, "other_opex": other}
        if locked:
            base = sum(m.base for m in ms)
            base_usd = sum(m.base / m.fx for m in ms)
            pool_usd = sum(m.pool / m.fx for m in ms)
            pool = sum(m.pool for m in ms)
            row.update(
                status="locked", base=base, pool=pool, mgmt=sum(m.mgmt for m in ms),
                debt=sum(m.debt for m in ms), tax_adj=sum(m.adj for m in ms),
                fx=base / base_usd if base_usd else None,
                annual_yield=base_usd * DAYS_IN_YEAR / days / capital,
                investor_annual_yield=pool_usd * DAYS_IN_YEAR / days / capital,
                pay_date=max((m.pay_date for m in ms if m.pay_date), default=None),
                pay_date_estimated=False)
        else:
            tax = -profit_tax if counts["forecast"] or counts["preliminary"] or counts["reconciled"] else 0.0
            adj = sum(m.adj for m in ms) + tax
            base = net - other + adj
            sp = split_base(base, fx_fallback, days, capital, tiers)
            row.update(
                status="estimate" if (counts["final"] + counts["preliminary"] + counts["reconciled"]) else "forecast",
                base=base, pool=sp.pool_uah, mgmt=sp.mgmt_uah, debt=None, tax_adj=adj, fx=fx_fallback,
                annual_yield=sp.annual_yield, investor_annual_yield=sp.investor_annual_yield,
                pay_date=quarter_end(y, q) + timedelta(days=lag_days), pay_date_estimated=True)
        out.append(row)
    return out


def run_all(reports: list[Report], prelims: list[Prelim], cfg: dict, today: date) -> dict:
    """Run base/low/high and merge. Returns months, quarters, calibration, assumptions."""
    fc = cfg.get("forecast", {})
    capital = float(cfg["capital_usd"])
    tiers = tiers_from_config(cfg["tiers"])
    k = fc.get("calibration") or calibration(reports, prelims)
    actual = build_months(reports, prelims, k, electricity_per_cycle(reports))
    assumptions = derive_assumptions(actual, reports, fc)

    qy, qq = quarter_key(today)
    for _ in range(int(fc.get("quarters_ahead", 1))):
        qy, qq = (qy + 1, 1) if qq == 4 else (qy, qq + 1)
    horizon = quarter_end(qy, qq)

    runs: dict[str, tuple[list[MonthRow], list[dict]]] = {}
    for scen in (assumptions or {"base": None}):
        a = assumptions.get(scen)
        months = extend_with_forecast(actual, a, horizon) if a else list(actual)
        fx = a.fx if a else (max(reports, key=lambda r: r.period_end).nbu_rate if reports else 1.0)
        runs[scen] = (months, build_quarters(months, capital, tiers, fx,
                                              float(fc.get("profit_tax_uah", 0)),
                                              int(fc.get("payout_lag_days", 25))))
    months, quarters = runs["base"]
    for scen in ("low", "high"):
        if scen not in runs:
            continue
        s_months, s_quarters = runs[scen]
        by_month = {m.month: m for m in s_months}
        for m in months:
            if m.status == "forecast":
                setattr(m, f"net_{scen}", by_month[m.month].net)
                setattr(m, f"gross_{scen}", by_month[m.month].gross)
        by_q = {r["label"]: r for r in s_quarters}
        for r in quarters:
            if r["status"] != "locked":
                s = by_q[r["label"]]
                r[scen] = {"base": s["base"], "pool": s["pool"], "annual_yield": s["annual_yield"],
                           "investor_annual_yield": s["investor_annual_yield"]}
    return {"months": months, "quarters": quarters, "calibration": k,
            "assumptions": assumptions, "tiers": tiers}


def check_reports(reports: list[Report], capital: float, tiers) -> list[dict]:
    """Consistency checks that catch typos in the company's reports or in manual entry."""
    out = []
    for r in reports:
        label = r.key
        calc_base = r.net_ops + r.prior_tax - r.other_opex - r.profit_tax
        d = calc_base - r.base
        out.append({"report": label, "name": "Дохід − витрати − податок = база", "ok": abs(d) < 1.0,
                    "detail": f"різниця {d:+,.2f} грн"})
        d = r.pool + r.mgmt - r.base
        out.append({"report": label, "name": "Пул інвесторів + винагорода КК = база", "ok": abs(d) < 1.0,
                    "detail": f"різниця {d:+,.2f} грн"})
        sp = split_base(r.base, r.nbu_rate, r.days, capital, tiers)
        rel = (sp.pool_uah - r.pool) / r.pool if r.pool else 0.0
        out.append({"report": label, "name": "Пул за шкалою розподілу збігається з фактом", "ok": abs(rel) < 0.001,
                    "detail": f"розрахунок {sp.pool_uah:,.0f} vs звіт {r.pool:,.0f} ({rel:+.2%})"})
        if r.reported_yield is not None:
            dy = sp.annual_yield - r.reported_yield
            out.append({"report": label, "name": "Дохідність проєкту у звіті збігається з розрахунком",
                        "ok": abs(dy) < 0.005,
                        "detail": f"розрахунок {sp.annual_yield:.2%} vs у звіті {r.reported_yield:.2%}"})
    return out
