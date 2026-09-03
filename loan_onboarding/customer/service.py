from __future__ import annotations

from loan_onboarding.customer import db
from loan_onboarding.customer.models import Customer, CustomerNotFound


async def find_by_identifier(applicant_identifier: str) -> Customer | None:
    """Read-only, no side effects."""
    record = await db.find_by_identifier(applicant_identifier)
    return Customer.from_record(record) if record is not None else None


async def get_or_create(
    applicant_identifier: str,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
) -> Customer:
    """Idempotent find-or-create. Called only from
    application/activities.py's persist_decision, on approval -- see
    CLAUDE.md's "Applying without being a customer yet". Never call this
    from bff_customer's identify step; use find_by_identifier there.

    `name`/`email`/`phone` seed the row on a genuine first create only
    -- see db.get_or_create's docstring. Use `update_profile` for an
    existing customer's later applications instead."""
    record = await db.get_or_create(applicant_identifier, name, email, phone)
    return Customer.from_record(record)


async def update_profile(customer_id: str, name: str | None, email: str | None, phone: str | None) -> Customer:
    """Unconditional overwrite of an existing customer's profile.
    Called only from application/activities.py's persist_decision, when
    a since-approved application's customer_id is already set -- see
    CLAUDE.md's "Returning-customer profile refresh and ID reuse"."""
    record = await db.update_profile(customer_id, name, email, phone)
    if record is None:
        raise CustomerNotFound(customer_id)
    return Customer.from_record(record)


async def get(customer_id: str) -> Customer:
    record = await db.get(customer_id)
    if record is None:
        raise CustomerNotFound(customer_id)
    return Customer.from_record(record)
