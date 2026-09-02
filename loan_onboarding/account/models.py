from __future__ import annotations

import asyncpg
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Account:
    account_id: UUID
    customer_id: UUID
    product_type: str
    opened_at: datetime
    status: Literal["ACTIVE", "CLOSED"]

    @classmethod
    def from_record(cls, record: asyncpg.Record) -> "Account":
        return cls(
            account_id=record["account_id"],
            customer_id=record["customer_id"],
            product_type=record["product_type"],
            opened_at=record["opened_at"],
            status=record["status"],
        )


class AccountNotFound(Exception):
    """Raised by service.get() when no account exists for the given id."""
