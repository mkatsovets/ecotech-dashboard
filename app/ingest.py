"""Turning the company's reports into data: chat messages (regex) and infographics (Claude vision).

Everything here only PROPOSES values. The admin page shows them in a form, runs the consistency
checks and the human confirms before anything is saved.
"""
from __future__ import annotations

import base64
import calendar
import json
import re
from datetime import date

# Ukrainian month stems (nominative and genitive share the stem): "липень" / "липня" -> "лип"
MONTH_STEMS = ["січ", "лют", "бер", "кві", "тра", "чер", "лип", "сер", "вер", "жов", "лис", "гру"]
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4}
NUMBER = r"(\d{1,3}(?:[\s  ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"


def _month_of(word: str) -> int | None:
    w = word.lower()
    for i, stem in enumerate(MONTH_STEMS, start=1):
        if w.startswith(stem):
            return i
    return None


def _num(s: str) -> float:
    return float(re.sub(r"[\s  ]", "", s).replace(",", "."))


# --- chat messages -----------------------------------------------------------------------------

def parse_chat(text: str) -> tuple[list[dict], list[str]]:
    """Parse one or several monthly messages. Returns (monthly rows, human-readable problems)."""
    parts = re.split(r"(?i)(?=Вітаю,?\s*інвестори)", text)
    parts = [p for p in parts if p.strip()]
    rows, problems = [], []
    for i, msg in enumerate(parts, start=1):
        label = f"Повідомлення {i}"
        m = re.search(r"(?i)за\s+([А-Яа-яІіЇїЄєҐґ]+)\s+(\d{4})\s*року", msg)
        month = _month_of(m.group(1)) if m else None
        if not m or month is None:
            problems.append(f"{label}: не знайдено місяць і рік («за … 2026 року»)")
            continue
        year = int(m.group(2))
        days = re.search(r"(?i)робочих\s+днів\s*[—–-]\s*(\d+)", msg)
        cycles = re.search(r"(?is)циклів.*?[—–-]\s*" + NUMBER, msg)
        amount = re.search(NUMBER + r"\s*грн", msg)
        if not amount:
            problems.append(f"{label} ({month:02d}.{year}): не знайдено суму доходу («… грн»)")
            continue
        if re.search(r"(?i)не\s+отримали\s+фінальну\s+звірку", msg) or re.search(r"(?i)попередн", msg):
            status = "preliminary"
        elif re.search(r"(?i)отримали\s+фінальну\s+звірку", msg):
            status = "reconciled"
        else:
            status = "preliminary"
        first_day = date(year, month, 1)
        rows.append({
            "month": first_day.isoformat(),
            "days": int(days.group(1)) if days else calendar.monthrange(year, month)[1],
            "cycles": _num(cycles.group(1)) if cycles else None,
            "net_uah": _num(amount.group(1)),
            "status": status,
            "note": "з чату" + ("" if days else " (кількість днів взято з календаря)"),
        })
    if not parts:
        problems.append("Порожній текст")
    return rows, problems


# --- period label / key ---------------------------------------------------------------------------

def parse_period_label(label: str) -> tuple[date, date] | None:
    """'ВЕРЕСЕНЬ 2026 РОКУ' -> (2026-09-01, 2026-09-30); 'ЛИПЕНЬ – СЕРПЕНЬ 2026' -> (07-01, 08-31);
    'II КВАРТАЛ 2026 РОКУ' -> (04-01, 06-30). The operator can still correct the dates."""
    year = re.search(r"(\d{4})", label)
    if not year:
        return None
    y = int(year.group(1))
    q = re.search(r"(?i)\b(IV|III|II|I)\s*квартал", label)
    if q:
        n = ROMAN[q.group(1).upper()]
        return date(y, 3 * n - 2, 1), date(y, 3 * n, calendar.monthrange(y, 3 * n)[1])
    months = [mm for w in re.findall(r"[А-Яа-яІіЇїЄєҐґ]+", label) if (mm := _month_of(w))]
    if not months:
        return None
    first, last = months[0], months[-1]
    return date(y, first, 1), date(y, last, calendar.monthrange(y, last)[1])


def report_key(start: date, end: date) -> str:
    """One month 'YYYY-MM', several months 'YYYY-MM-MM' (the same shape as the seeded keys),
    a whole calendar quarter 'YYYY-Qn'."""
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.year}-{start.month:02d}"
    if start.year == end.year and (start.month - 1) % 3 == 0 and end.month == start.month + 2:
        return f"{start.year}-Q{(start.month - 1) // 3 + 1}"
    return f"{start.year}-{start.month:02d}-{end.month:02d}"


# --- infographic -> report fields (Claude vision) ---------------------------------------------------

MODEL = "claude-opus-5-5"
ALLOWED_MEDIA = {"image/png", "image/jpeg", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024        # the API's per-image limit

_NUM = {"anyOf": [{"type": "number"}, {"type": "null"}]}
_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["period_label", "worked_days", "rate_uah_per_usd", "rate_date_text", "revenue", "prior_tax",
                 "opex_total", "opex_electricity", "opex_personnel", "opex_software", "profit_tax", "base",
                 "pool", "pool_ukrenergo_debt", "mgmt", "project_yield_percent", "investor_yield_percent",
                 "cycles", "uncertain_fields"],
    "properties": {
        "period_label": {**_STR, "description": "text under 'ПІДСУМКИ РОБОТИ ЗА', e.g. 'ВЕРЕСЕНЬ 2026 РОКУ'"},
        "worked_days": {**_NUM, "description": "number of worked days in the period"},
        "rate_uah_per_usd": {**_NUM, "description": "the exchange rate used in the report, UAH per USD"},
        "rate_date_text": {**_STR, "description": "date of that rate as printed"},
        "revenue": {**_NUM, "description": "row 1, income received by the cooperative, UAH without VAT"},
        "prior_tax": {**_NUM, "description": "previously withheld profit tax returned (added to income), UAH; null if absent"},
        "opex_total": {**_NUM, "description": "row 2, operating costs without the management company fee"},
        "opex_electricity": {**_NUM, "description": "sub-row: electricity costs"},
        "opex_personnel": {**_NUM, "description": "sub-row: personnel costs"},
        "opex_software": {**_NUM, "description": "sub-row: software, internet and bank costs"},
        "profit_tax": {**_NUM, "description": "row 3, corporate profit tax"},
        "base": {**_NUM, "description": "row 4, base for calculating payouts"},
        "pool": {**_NUM, "description": "row 5, accrued to investors through buyback (UAH column)"},
        "pool_ukrenergo_debt": {**_NUM, "description": "'у т.ч. Борг УкрЕнерго' under row 5 (UAH column); null if absent"},
        "mgmt": {**_NUM, "description": "row 6, management company fee (UAH column)"},
        "project_yield_percent": {**_NUM, "description": "total project profit/yield in percent per year as printed"},
        "investor_yield_percent": {**_NUM, "description": "investor profit in percent per year as printed"},
        "cycles": {**_NUM, "description": "number of cycles if printed, else null"},
        "uncertain_fields": {"type": "array", "items": {"type": "string"},
                             "description": "names of fields you could not read reliably"},
    },
}
PROMPT = (
    "This is an investor report infographic of an energy-storage cooperative (Ukrainian). "
    "Read the values EXACTLY as printed. Amounts are UAH without VAT; if the table has a UAH and a USD "
    "column, take the UAH column. Write numbers as plain numbers: no spaces, a dot as decimal separator, "
    "percent values without the % sign. Do not calculate, correct or infer anything: if a value is not "
    "printed, use null. List every field you are not sure about in uncertain_fields."
)


def extract_report_from_image(data: bytes, media_type: str, client=None, model: str = MODEL) -> dict:
    """Ask Claude to read the infographic. Returns prefill values for the report form:
    {'fields': {...ReportIn-like...}, 'uncertain': [...], 'raw': {...}}.
    Raises ValueError on unusable input / refusal; anthropic errors propagate."""
    if media_type not in ALLOWED_MEDIA:
        raise ValueError("Підтримуються лише PNG, JPEG, WEBP")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Зображення більше 5 МБ")
    if client is None:
        import anthropic
        client = anthropic.Anthropic()          # reads ANTHROPIC_API_KEY
    response = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",                      # a policy decline is re-run on a fallback model
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.standard_b64encode(data).decode("ascii")}},
            {"type": "text", "text": PROMPT},
        ]}],
    )
    if response.stop_reason == "refusal":
        raise ValueError("Модель відмовилась обробити зображення")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Модель повернула відповідь не у форматі JSON") from exc
    return map_extracted(raw)


def _pct(v):
    return None if v is None else v / 100.0


def map_extracted(raw: dict) -> dict:
    """Raw model output -> form prefill (dates guessed from the period label)."""
    period = parse_period_label(raw.get("period_label") or "")
    fields = {
        "period_start": period[0].isoformat() if period else None,
        "period_end": period[1].isoformat() if period else None,
        "days": int(raw["worked_days"]) if raw.get("worked_days") is not None else None,
        "cycles": raw.get("cycles"),
        "revenue": raw.get("revenue"),
        "prior_tax": raw.get("prior_tax") or 0,
        "opex_electricity": raw.get("opex_electricity"),
        "opex_personnel": raw.get("opex_personnel"),
        "opex_software": raw.get("opex_software"),
        "profit_tax": raw.get("profit_tax") or 0,
        "base": raw.get("base"),
        "pool": raw.get("pool"),
        "debt_pool": raw.get("pool_ukrenergo_debt") or 0,
        "mgmt": raw.get("mgmt"),
        "nbu_rate": raw.get("rate_uah_per_usd"),
        "pay_date": None,                                     # not printed on the infographic
        "reported_yield": _pct(raw.get("project_yield_percent")),
        "reported_investor_yield": _pct(raw.get("investor_yield_percent")),
        "note": ("Курс на " + raw["rate_date_text"]) if raw.get("rate_date_text") else "",
    }
    if period:
        fields["key"] = report_key(*period)
    # the cost lines must add up to the printed total; a mismatch means a misread digit
    uncertain = list(raw.get("uncertain_fields") or [])
    parts = [raw.get(k) for k in ("opex_electricity", "opex_personnel", "opex_software")]
    if raw.get("opex_total") is not None and all(p is not None for p in parts):
        if abs(sum(parts) - raw["opex_total"]) > 1.0:
            uncertain.append("opex_electricity/opex_personnel/opex_software: сума не збігається з «Операційні витрати»")
    return {"fields": fields, "uncertain": uncertain, "raw": raw}
