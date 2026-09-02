from __future__ import annotations

import asyncpg
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Customer:
    customer_id: UUID
    applicant_identifier: str
    name: str | None
    email: str | None
    phone: str | None
    created_at: datetime

    @classmethod
    def from_record(cls, record: asyncpg.Record) -> "Customer":
        return cls(
            customer_id=record["customer_id"],
            applicant_identifier=record["applicant_identifier"],
            name=record["name"],
            email=record["email"],
            phone=record["phone"],
            created_at=record["created_at"],
        )


class CustomerNotFound(Exception):
    """Raised by service.get() when no customer exists for the given id."""
