"""Investor return metrics (pure functions)."""
from __future__ import annotations

from datetime import date

YEAR = 365.25  # same convention as the owner's Excel tracker


def annualized(amount_usd: float, invested_usd: float, days: int) -> float | None:
    """Run-rate annual yield of one payout: (amount / invested) * 365.25 / days."""
    if invested_usd <= 0 or days <= 0:
        return None
    return amount_usd / invested_usd * YEAR / days


def xirr(flows: list[tuple[date, float]]) -> float | None:
    """XIRR by bisection. Returns None if there is no sign change or no root."""
    if len(flows) < 2:
        return None
    if not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None
    t0 = min(d for d, _ in flows)

    def npv(r: float) -> float:
        return sum(a / (1.0 + r) ** ((d - t0).days / 365.0) for d, a in flows)

    lo, hi = -0.9999, 100.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(300):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if f_lo * f_mid <= 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2
