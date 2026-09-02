from __future__ import annotations

from loan_onboarding.account import db
from loan_onboarding.account.models import Account, AccountNotFound


async def create_account(customer_id: str, product_type: str, application_id: str) -> Account:
    """Always creates a new row -- no find-or-create semantics. Called
    only from application/activities.py's persist_decision, exactly
    once per application that reaches terminal APPROVED -- see
    CLAUDE.md's "Applying without being a customer yet" for the
    idempotency guard (get_by_application_id) that must run before
    calling this."""
    record = await db.create(customer_id, product_type, application_id)
    return Account.from_record(record)


async def has_active_account_of_type(customer_id: str, product_type: str) -> bool:
    """Read-only. Called by application.service.check_decision_allowed
    before an Approve decision is signaled, never directly by a BFF."""
    return await db.has_active_account_of_type(customer_id, product_type)


async def get(account_id: str) -> Account:
    record = await db.get(account_id)
    if record is None:
        raise AccountNotFound(account_id)
    return Account.from_record(record)


async def get_by_application_id(application_id: str) -> Account | None:
    """Read-only. The reverse lookup the account-to-application
    direction flip exists to make possible -- also what
    persist_decision calls first, as its idempotency check. Called by
    bff_backoffice's review dialog to render an application's resulting
    account."""
    record = await db.get_by_application_id(application_id)
    return Account.from_record(record) if record is not None else None
