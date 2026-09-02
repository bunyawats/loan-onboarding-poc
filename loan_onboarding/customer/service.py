from __future__ import annotations

from loan_onboarding.customer import db
from loan_onboarding.customer.models import Customer, CustomerNotFound


async def find_by_identifier(applicant_identifier: str) -> Customer | None:
    """Read-only, no side effects."""
    record = await db.find_by_identifier(applicant_identifier)
    return Customer.from_record(record) if record is not None else None


async def get_or_create(applicant_identifier: str) -> Customer:
    """Idempotent find-or-create. Called only from
    application/activities.py's persist_decision, on approval -- see
    CLAUDE.md's "Applying without being a customer yet". Never call this
    from bff_customer's identify step; use find_by_identifier there."""
    record = await db.get_or_create(applicant_identifier)
    return Customer.from_record(record)


async def get(customer_id: str) -> Customer:
    record = await db.get(customer_id)
    if record is None:
        raise CustomerNotFound(customer_id)
    return Customer.from_record(record)
