"""Admin side: add / edit reports, monthly figures and UkrEnergo debt repayments.

Protected by HTTP Basic auth (user `admin`, password from ADMIN_PASSWORD; the admin side is
disabled when it is not set). Every change is written to the audit log. Mutating calls need the
`X-Admin: 1` header, so a form on another site cannot trigger them with the cached credentials.
"""
from __future__ import annotations

import base64
import binascii
import os
import re
import secrets
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, model_validator

from .ingest import MAX_IMAGE_BYTES, extract_report_from_image, parse_chat, report_key
from .model import Report, check_reports, month_overlaps
from .waterfall import tiers_from_config

STATIC = Path(__file__).resolve().parent / "static"


class ReportIn(BaseModel):
    key: str | None = Field(None, max_length=40, pattern=r"^[0-9A-Za-z_\-]+$")
    period_start: date
    period_end: date
    days: int = Field(gt=0, le=400)
    cycles: float | None = Field(None, ge=0, le=100000)
    revenue: float = Field(ge=0)
    prior_tax: float = Field(0, ge=0)
    opex_electricity: float = Field(ge=0)
    opex_personnel: float = Field(ge=0)
    opex_software: float = Field(ge=0)
    profit_tax: float = Field(0, ge=0)
    base: float
    pool: float = Field(ge=0)
    debt_pool: float = Field(0, ge=0)
    mgmt: float = Field(ge=0)
    nbu_rate: float = Field(gt=0, lt=1000)
    pay_date: date | None = None
    reported_yield: float | None = Field(None, ge=0, le=20)           # fractions, 0.343 = 34.3%
    reported_investor_yield: float | None = Field(None, ge=0, le=20)
    note: str = Field("", max_length=500)

    @model_validator(mode="after")
    def _consistent(self):
        if self.period_end < self.period_start:
            raise ValueError("Кінець періоду раніше за початок")
        if self.debt_pool > self.pool:
            raise ValueError("Борг УкрЕнерго більший за суму до виплати інвесторам")
        return self


class MonthlyIn(BaseModel):
    month: date
    days: int = Field(gt=0, le=31)
    cycles: float | None = Field(None, ge=0, le=1000)
    net_uah: float = Field(gt=0)
    status: Literal["preliminary", "reconciled"] = "preliminary"
    note: str = Field("", max_length=500)


class RepaymentIn(BaseModel):
    day: date
    pool_uah: float = Field(gt=0)
    note: str = Field("", max_length=500)


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=30000)


class ImageIn(BaseModel):
    filename: str = Field(max_length=200)
    media_type: str
    data_base64: str


def make_router(repo, cfg: dict, upload_dir: Path,
                get_password: Callable[[], str | None] = lambda: os.environ.get("ADMIN_PASSWORD"),
                get_api_key: Callable[[], str | None] = lambda: os.environ.get("ANTHROPIC_API_KEY"),
                client_factory: Callable[[], object] | None = None) -> APIRouter:
    pid = cfg["id"]
    capital = float(cfg["capital_usd"])
    tiers = tiers_from_config(cfg["tiers"])
    basic = HTTPBasic(auto_error=False)
    router = APIRouter()

    def auth(request: Request, creds: HTTPBasicCredentials | None = Depends(basic)) -> None:
        password = get_password()
        if not password:
            raise HTTPException(503, "Адмінку вимкнено: на сервері не задано ADMIN_PASSWORD")
        ok = (creds is not None
              and secrets.compare_digest(creds.username.encode(), b"admin")
              and secrets.compare_digest(creds.password.encode(), password.encode()))
        if not ok:
            time.sleep(0.5)                                   # slows down password guessing
            raise HTTPException(401, "Потрібен вхід", headers={"WWW-Authenticate": 'Basic realm="Ecotech admin"'})
        if request.method not in ("GET", "HEAD") and request.headers.get("x-admin") != "1":
            raise HTTPException(403, "Відсутній заголовок X-Admin")

    guard = [Depends(auth)]

    def _row(model: BaseModel) -> dict:
        return model.model_dump(mode="json")

    def _report_obj(r: dict) -> Report:
        d = {k: v for k, v in r.items() if k not in ("kind", "source")}     # DB rows carry both
        d.update(period_start=date.fromisoformat(r["period_start"]), period_end=date.fromisoformat(r["period_end"]),
                 pay_date=date.fromisoformat(r["pay_date"]) if r.get("pay_date") else None)
        return Report(kind="final", **d)

    def _checks(r: dict) -> list[dict]:
        return check_reports([_report_obj(r)], capital, tiers)

    def _overlaps(r: dict, key: str) -> list[str]:
        months = {m for m, _ in month_overlaps(date.fromisoformat(r["period_start"]),
                                               date.fromisoformat(r["period_end"]))}
        return [o["key"] for o in repo.list_rows("report", pid) if o["key"] != key and months & {
            m for m, _ in month_overlaps(date.fromisoformat(o["period_start"]), date.fromisoformat(o["period_end"]))}]

    # --- page and data ------------------------------------------------------------------------
    @router.get("/admin", dependencies=guard, include_in_schema=False)
    def admin_page():
        return FileResponse(STATIC / "admin.html")

    @router.get("/api/admin/data", dependencies=guard)
    def data():
        reports = repo.list_rows("report", pid)
        return {"reports": reports, "monthly": repo.list_rows("monthly", pid),
                "repayments": repo.list_rows("repayment", pid), "audit": repo.audit_tail(pid),
                "checks": {r["key"]: _checks(r) for r in reports},
                "claude_enabled": bool(get_api_key())}

    # --- final reports ----------------------------------------------------------------------------
    @router.post("/api/admin/report/check", dependencies=guard)
    def report_check(body: ReportIn):
        r = _row(body)
        key = body.key or report_key(body.period_start, body.period_end)
        return {"key": key, "checks": _checks(r), "overlaps": _overlaps(r, key)}

    @router.post("/api/admin/report", dependencies=guard)
    def report_save(body: ReportIn):
        r = _row(body)
        r["kind"] = "final"
        r["key"] = key = body.key or report_key(body.period_start, body.period_end)
        clash = _overlaps(r, key)
        if clash:
            raise HTTPException(409, f"Період перетинається зі звітом {', '.join(clash)}: місяці були б пораховані двічі")
        before = repo.get_row("report", pid, key)
        repo.put_row("report", pid, r, "admin")
        repo.audit("update" if before else "create", "report", pid, key, before, repo.get_row("report", pid, key))
        return {"key": key, "created": before is None, "checks": _checks(r)}

    @router.delete("/api/admin/report/{key}", dependencies=guard)
    def report_delete(key: str):
        before = repo.get_row("report", pid, key)
        if before is None:
            raise HTTPException(404, "Звіт не знайдено")
        repo.delete_row("report", pid, key)
        repo.audit("delete", "report", pid, key, before, None)
        return {"deleted": key}

    # --- monthly figures --------------------------------------------------------------------------
    @router.post("/api/admin/monthly", dependencies=guard)
    def monthly_save(body: list[MonthlyIn]):
        saved = []
        for item in body:
            row = _row(item)
            row["month"] = item.month.replace(day=1).isoformat()
            before = repo.get_row("monthly", pid, row["month"])
            repo.put_row("monthly", pid, row, "admin")
            repo.audit("update" if before else "create", "monthly", pid, row["month"], before,
                       repo.get_row("monthly", pid, row["month"]))
            saved.append(row["month"])
        return {"saved": saved}

    @router.delete("/api/admin/monthly/{month}", dependencies=guard)
    def monthly_delete(month: str):
        before = repo.get_row("monthly", pid, month)
        if before is None:
            raise HTTPException(404, "Місяць не знайдено")
        repo.delete_row("monthly", pid, month)
        repo.audit("delete", "monthly", pid, month, before, None)
        return {"deleted": month}

    # --- UkrEnergo debt repayments -----------------------------------------------------------------
    @router.post("/api/admin/repayment", dependencies=guard)
    def repayment_save(body: RepaymentIn):
        row = _row(body)
        before = repo.get_row("repayment", pid, row["day"])
        repo.put_row("repayment", pid, row, "admin")
        repo.audit("update" if before else "create", "repayment", pid, row["day"], before,
                   repo.get_row("repayment", pid, row["day"]))
        return {"saved": row["day"]}

    @router.delete("/api/admin/repayment/{day}", dependencies=guard)
    def repayment_delete(day: str):
        before = repo.get_row("repayment", pid, day)
        if before is None:
            raise HTTPException(404, "Погашення не знайдено")
        repo.delete_row("repayment", pid, day)
        repo.audit("delete", "repayment", pid, day, before, None)
        return {"deleted": day}

    # --- helpers that read the company's reports ---------------------------------------------------
    @router.post("/api/admin/parse-chat", dependencies=guard)
    def chat_parse(body: ChatIn):
        rows, problems = parse_chat(body.text)
        return {"rows": rows, "problems": problems}

    @router.post("/api/admin/parse-image", dependencies=guard)
    def image_parse(body: ImageIn):
        if not get_api_key():
            raise HTTPException(503, "Розпізнавання вимкнено: на сервері не задано ANTHROPIC_API_KEY")
        try:
            data = base64.b64decode(body.data_base64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(400, "Некоректні дані зображення")
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "Зображення більше 5 МБ")
        client = client_factory() if client_factory else None
        try:
            result = extract_report_from_image(data, body.media_type, client=client)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        except Exception as exc:                              # network / API errors from the SDK
            raise HTTPException(502, f"Помилка Claude API: {type(exc).__name__}")
        # keep the original picture next to the database: the audit trail for what was typed in
        upload_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^0-9A-Za-z._-]", "_", body.filename)[:80] or "report"
        saved = upload_dir / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{safe}"
        saved.write_bytes(data)
        result["saved_as"] = saved.name
        return result

    return router
