from datetime import date
from pathlib import Path

import pytest

from app.db import Repo, load_config, load_seed

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 11, 20)


@pytest.fixture(scope="session")
def cfg():
    return load_config(ROOT / "config" / "projects" / "eti1.yaml")


@pytest.fixture()
def repo(cfg):
    r = Repo(":memory:")
    load_seed(r, cfg["id"], ROOT / "data" / "seed" / "eti1.yaml")
    return r


class FakeFx:
    """Rates as used in the owner's Excel tracker (Privat card sell)."""
    RATES = {date(2026, 7, 24): 45.0451, date(2026, 9, 25): 45.0451, date(2026, 10, 27): 45.8716}

    def get(self, day):
        return self.RATES.get(day, 45.4546)
