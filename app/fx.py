"""Privatbank exchange rates from index.minfin.com.ua (archive page per date).

The page has two zebra tables: cash rates and card-conversion rates, each with a
USD row "buy / sell". Rates are cached in SQLite, one request per uncached date.
"""
from __future__ import annotations

import re
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

_MISS: dict[date, float] = {}   # day -> monotonic time of the last failed fetch
MISS_TTL = 1800                 # do not retry a failed day for 30 minutes
URL = "https://index.minfin.com.ua/ua/exchange/archive/privat/{d}/"
USER_AGENT = "ecotech-dashboard/0.1 (personal investor dashboard)"
KINDS = ("cash_buy", "cash_sell", "card_buy", "card_sell")


class FxError(RuntimeError):
    pass


def parse_privat_html(html: str) -> dict[str, float]:
    """Extract USD rates from the page: {cash_buy, cash_sell, card_buy, card_sell}."""
    out: dict[str, float] = {}
    for m in re.finditer(r"<table[^>]*class=.zebra.*?</table>", html, flags=re.S):
        text = re.sub(r"<[^>]+>", "|", m.group(0))
        text = re.sub(r"\|+", "|", text).replace("&nbsp;", " ")
        if "карт" in text:
            prefix = "card"
        elif "готівков" in text:
            prefix = "cash"
        else:
            continue
        row = re.search(r"\|USD\|(.*?)(?:\|EUR\||$)", text, flags=re.S)
        if not row:
            continue
        nums = re.findall(r"\d+,\d{4}", row.group(1))  # rates have 4 decimals; deltas use dots
        if len(nums) >= 2:
            out[f"{prefix}_buy"] = float(nums[0].replace(",", "."))
            out[f"{prefix}_sell"] = float(nums[1].replace(",", "."))
    if len(out) != 4:
        raise FxError(f"could not parse Privatbank USD rates (got {sorted(out)})")
    return out


def fetch_privat(day: date, timeout: int = 20) -> dict[str, float]:
    req = urllib.request.Request(URL.format(d=day.isoformat()), headers={"User-Agent": USER_AGENT})
    try:
        html = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
    except Exception as exc:  # network/HTTP errors
        raise FxError(f"fetch failed for {day}: {exc}") from exc
    return parse_privat_html(html)


class FxService:
    """Cached rate lookup. `repo` must provide get_fx(day) / put_fx(day, rates)."""

    def __init__(self, repo, kind: str = "card_sell", fetcher=fetch_privat, today: date | None = None):
        if kind not in KINDS:
            raise ValueError(f"unknown rate kind {kind}")
        self.repo, self.kind, self.fetcher = repo, kind, fetcher
        self.today = today or date.today()
        self._last_fetch = 0.0

    def _load(self, day: date) -> dict[str, float] | None:
        """Cached rates for exactly `day`, fetching once if needed."""
        cached = self.repo.get_fx(day)
        if cached is not None:
            return cached
        if time.monotonic() - _MISS.get(day, -1e9) < MISS_TTL:
            return None                      # recently failed (e.g. today's rate not yet published)
        wait = 0.6 - (time.monotonic() - self._last_fetch)  # be polite to the site
        if wait > 0:
            time.sleep(wait)
        try:
            cached = self.fetcher(day)
        except FxError:
            _MISS[day] = time.monotonic()
            return None
        finally:
            self._last_fetch = time.monotonic()
        self.repo.put_fx(day, cached, datetime.now(timezone.utc).isoformat())
        return cached

    def get(self, day: date) -> float | None:
        """Rate for `day`; future dates use today's. If the page has no data for the day
        (weekend, not yet published), the last available earlier rate is used."""
        day = min(day, self.today)
        for back in range(0, 8):
            d = day - timedelta(days=back)
            rates = self._load(d)
            if rates is not None:
                if back and day < self.today:  # remember the carry-over for past days only
                    self.repo.put_fx(day, rates, datetime.now(timezone.utc).isoformat())
                return rates.get(self.kind)
        return None
