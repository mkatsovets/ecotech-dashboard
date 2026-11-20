"""Profit distribution waterfall (pure functions, no I/O)."""
from __future__ import annotations

from dataclasses import dataclass

DAYS_IN_YEAR = 365  # the cooperative annualises with 365 (verified against reports)


def tiers_from_config(raw: list[dict]) -> list[tuple[float, float]]:
    """[{upto: 0.12, investor: 1.0}, {upto: null, ...}] -> [(0.12, 1.0), (inf, 0.4)]"""
    return [(float("inf") if t["upto"] is None else float(t["upto"]), float(t["investor"])) for t in raw]


def investor_yield(project_yield: float, tiers: list[tuple[float, float]]) -> float:
    """Investor's annual yield for a given annualised project yield."""
    total, lower = 0.0, 0.0
    for upper, share in tiers:
        slice_ = min(project_yield, upper) - lower
        if slice_ > 0:
            total += slice_ * share
        lower = upper
        if project_yield <= upper:
            break
    return total


@dataclass(frozen=True)
class Split:
    annual_yield: float           # annualised project yield (USD)
    investor_annual_yield: float
    investor_ratio: float         # investor pool / base
    base_uah: float
    pool_uah: float               # to all investors
    mgmt_uah: float               # management company


def split_base(base_uah: float, rate: float, days: int, capital_usd: float,
               tiers: list[tuple[float, float]]) -> Split:
    """Split a period's distribution base between investors and the management company."""
    if base_uah <= 0 or days <= 0 or rate <= 0:
        return Split(0.0, 0.0, 0.0, base_uah, 0.0, 0.0)
    y = base_uah / rate * DAYS_IN_YEAR / days / capital_usd
    inv_y = investor_yield(y, tiers)
    ratio = inv_y / y
    pool = base_uah * ratio
    return Split(y, inv_y, ratio, base_uah, pool, base_uah - pool)
