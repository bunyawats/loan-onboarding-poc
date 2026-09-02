from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg


@dataclass(frozen=True, slots=True)
class Application:
    application_id: UUID
    applicant_identifier: str
    customer_id: UUID | None
    account_id: UUID | None
    workflow_id: str | None
    product_type: str
    payload: dict[str, Any]
    applicant_name: str
    applicant_email: str
    applicant_phone: str
    amount: Decimal
    status: str
    underwriter_name: str | None
    underwriter_comment: str | None
    underwriter_decided_at: datetime | None
    manager_name: str | None
    manager_comment: str | None
    manager_decided_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: asyncpg.Record) -> "Application":
        return cls(**{f.name: record[f.name] for f in fields(cls)})


class ApplicationNotFound(Exception):
    """Raised by service.get() when no application exists for the given id."""
