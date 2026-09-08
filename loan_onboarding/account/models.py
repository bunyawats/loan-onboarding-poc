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
    # Closure request/decision tracking (Phase 18, "Account closure" --
    # see CLAUDE.md). All None until a closure is ever requested; only
    # the *current* request's data is kept, same as applications' own
    # decision columns -- a second request after a rejection overwrites
    # these rather than preserving history.
    closure_workflow_id: Optional[str] = None
    closure_requested_at: Optional[datetime] = None
    closure_decision_comment: Optional[str] = None
    closure_decided_by: Optional[str] = None
    closure_decided_at: Optional[datetime] = None

    @classmethod
    def from_record(cls, record: asyncpg.Record) -> "Account":
        return cls(
            account_id=record["account_id"],
            customer_id=record["customer_id"],
            application_id=record["application_id"],
            product_type=record["product_type"],
            opened_at=record["opened_at"],
            status=record["status"],
            closure_workflow_id=record["closure_workflow_id"],
            closure_requested_at=record["closure_requested_at"],
            closure_decision_comment=record["closure_decision_comment"],
            closure_decided_by=record["closure_decided_by"],
            closure_decided_at=record["closure_decided_at"],
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
