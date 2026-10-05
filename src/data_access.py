from __future__ import annotations

import copy
import csv
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


# ---------------------------------------------------------------------------
# Original starter helpers (kept for backwards compatibility).
# NOTE: pandas turns blank cells into NaN, which is *truthy* - e.g. a missing
# `security_review_date` would pass an `if row[...]` check. The copilot uses
# `DataRepository` below, which normalises blanks to None.
# ---------------------------------------------------------------------------
def load_employees() -> pd.DataFrame:
    return pd.read_csv(DATA_DIR / "employees.csv")


def load_budgets() -> pd.DataFrame:
    return pd.read_csv(DATA_DIR / "department_budgets.csv")


def load_software_catalog() -> pd.DataFrame:
    return pd.read_csv(DATA_DIR / "software_catalog.csv")


def load_vendors() -> pd.DataFrame:
    return pd.read_csv(DATA_DIR / "vendors.csv")


def load_purchase_history() -> pd.DataFrame:
    return pd.read_csv(DATA_DIR / "purchase_history.csv")


def load_requests() -> list[dict]:
    return json.loads((DATA_DIR / "requests.json").read_text(encoding="utf-8"))


def get_request(request_id: str) -> dict:
    for request in load_requests():
        if request["request_id"] == request_id:
            return request
    raise KeyError(f"Unknown request_id: {request_id}")


def load_policy_text() -> str:
    return (DATA_DIR / "procurement_policy.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Normalised repository used by the copilot tools.
# ---------------------------------------------------------------------------
_INT_FIELDS = {
    "annual_software_budget_usd", "committed_usd", "available_usd",
    "annual_cost_usd", "licensed_seats", "annual_amount_usd",
}


def _clean(row: dict[str, str]) -> dict:
    out: dict = {}
    for key, value in row.items():
        value = value.strip() if isinstance(value, str) else value
        if value == "" or value is None:
            out[key] = None
        elif key in _INT_FIELDS:
            try:
                out[key] = float(value) if "." in value else int(value)
            except ValueError:
                out[key] = None
        else:
            out[key] = value
    return out


def _read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return [_clean(r) for r in csv.DictReader(f)]


@dataclass
class DataRepository:
    """In-memory, normalised view of the business data.

    Evaluation scenarios can construct a repository with overrides (extra
    requests, modified vendor records, etc.) without touching files on disk.
    """

    employees: list[dict]
    budgets: list[dict]
    catalog: list[dict]
    vendors: list[dict]
    purchase_history: list[dict]
    requests: list[dict]
    policy_text: str
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_disk(cls, data_dir: Path = DATA_DIR) -> "DataRepository":
        return cls(
            employees=_read_csv(data_dir / "employees.csv"),
            budgets=_read_csv(data_dir / "department_budgets.csv"),
            catalog=_read_csv(data_dir / "software_catalog.csv"),
            vendors=_read_csv(data_dir / "vendors.csv"),
            purchase_history=_read_csv(data_dir / "purchase_history.csv"),
            requests=json.loads((data_dir / "requests.json").read_text(encoding="utf-8")),
            policy_text=(data_dir / "procurement_policy.md").read_text(encoding="utf-8"),
        )

    def clone(self) -> "DataRepository":
        return copy.deepcopy(self)

    # -- lookups -----------------------------------------------------------
    def get_request(self, request_id: str) -> dict | None:
        return next((copy.deepcopy(r) for r in self.requests if r.get("request_id") == request_id), None)

    def upsert_request(self, request: dict) -> None:
        self.requests = [r for r in self.requests if r.get("request_id") != request.get("request_id")]
        self.requests.append(copy.deepcopy(request))

    def get_employee(self, employee_id: str | None) -> dict | None:
        if not employee_id:
            return None
        return next((dict(e) for e in self.employees if e["employee_id"] == employee_id), None)

    def get_budget(self, department: str | None) -> dict | None:
        if not department:
            return None
        return next((dict(b) for b in self.budgets if b["department"].lower() == department.lower()), None)

    def get_vendor(self, vendor_name: str | None) -> dict | None:
        if not vendor_name:
            return None
        return next((dict(v) for v in self.vendors if v["vendor_name"].lower() == vendor_name.lower()), None)


@lru_cache(maxsize=1)
def _default_repository() -> DataRepository:
    return DataRepository.from_disk()


def default_repository() -> DataRepository:
    """A fresh copy of the on-disk data (callers may mutate it safely)."""
    return _default_repository().clone()
