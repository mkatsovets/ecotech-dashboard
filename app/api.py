"""FastAPI app: public read-only dashboard + JSON API."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse

from .db import Repo, load_config, load_seed
from .fx import FxService
from .service import build_dashboard

ROOT = Path(__file__).resolve().parent.parent
PROJECT = os.environ.get("ECOTECH_PROJECT", "eti1")
DB_PATH = Path(os.environ.get("ECOTECH_DB", ROOT / "data" / "ecotech.db"))

cfg = load_config(ROOT / "config" / "projects" / f"{PROJECT}.yaml")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
repo = Repo(DB_PATH)
load_seed(repo, cfg["id"], ROOT / "data" / "seed" / f"{PROJECT}.yaml")

app = FastAPI(title="Ecotech Invest dashboard")


@app.get("/api/dashboard")
def dashboard(amount: float | None = Query(None, gt=0, le=1e9),
              share: float | None = Query(None, gt=0, le=1),
              start: date | None = None,
              valuation: float | None = Query(None, ge=0, le=1e10)):
    today = date.today()
    fx = FxService(repo, kind=cfg["fx"]["investor_kind"], today=today)
    return build_dashboard(repo, fx, cfg, today, amount, share, start, valuation)


@app.get("/healthz")
def healthz():
    repo.conn.execute("SELECT 1")  # DB reachable
    return {"status": "ok"}


@app.get("/")
def index():
    return FileResponse(ROOT / "app" / "static" / "index.html")
