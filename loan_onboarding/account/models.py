from __future__ import annotations

import asyncpg
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

# Deliberately no import from workflow/ here -- this file otherwise has zero
# cross-module dependencies (just asyncpg/dataclasses/stdlib), and anything
# importing Account would transitively pull in temporalio via
# workflow/workflows.py just to satisfy one import-time assert. That assert
# (checking this Literal against workflow.workflows's own STATUS_ACCOUNT_*
# constants) lives in account/service.py instead, which already imports
# workflow/ for other reasons.
AccountStatus = Literal["ACTIVE", "CLOSURE_REQUESTED", "CLOSED"]


@dataclass(frozen=True, slots=True)
class Account:
    account_id: str
    customer_id: str
    application_id: str
    product_type: str
    opened_at: datetime
    status: AccountStatus

    @classmethod
    def from_record(cls, record: asyncpg.Record) -> "Account":
        return cls(
            account_id=record["account_id"],
            customer_id=record["customer_id"],
            application_id=record["application_id"],
            product_type=record["product_type"],
            opened_at=record["opened_at"],
            status=record["status"],
        )


ClosureRequestStatus = Literal["PENDING", "APPROVED", "REJECTED", "CANCELLED"]


@dataclass(frozen=True, slots=True)
class AccountClosureRequest:
    """One row per closure request against an account (Phase 22,
    "Account closure request history (1:M)" -- see CLAUDE.md /
    IMPLEMENTATION_PLAN.md). Replaces the single-current-request
    closure_* fields that used to live on `Account` itself -- a given
    account can have many of these over its lifetime (reject/cancel,
    then request again), each independently preserved."""

    closure_request_id: str
    account_id: str
    workflow_id: str
    workflow_run_id: Optional[str]
    requested_at: datetime
    status: ClosureRequestStatus
    decision_comment: Optional[str] = None
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None

    @classmethod
    def from_record(cls, record: asyncpg.Record) -> "AccountClosureRequest":
        return cls(
            closure_request_id=record["closure_request_id"],
            account_id=record["account_id"],
            workflow_id=record["workflow_id"],
            workflow_run_id=record["workflow_run_id"],
            requested_at=record["requested_at"],
            status=record["status"],
            decision_comment=record["decision_comment"],
            decided_by=record["decided_by"],
            decided_at=record["decided_at"],
        )


class AccountNotFound(Exception):
    """Raised by service.get() when no account exists for the given id."""


class AccountNotActive(Exception):
    """Raised by service.request_closure() when the account isn't
    currently ACTIVE -- e.g. a stale UI or a direct call made after a
    closure request is already pending or the account is already
    CLOSED. Same "the UI hides it, the service still enforces it"
    discipline application.service.create_application's product-type
    picker already follows for its own hard elimination."""
