"""`application/`'s public API -- the loan application entity and the
submission business rule (PRD §6.4's document-completeness gate).

Reads only from `customer/`/`account/` (`find_by_identifier`,
`has_active_account_of_type`) -- writes to either happen only in
`application/activities.py`, on approval (CLAUDE.md's "Applying without
being a customer yet")."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from decimal import Decimal
from typing import Any, Callable, Optional
from uuid import UUID

import asyncpg
from temporalio.client import Client

from loan_onboarding.account import service as account_service
from loan_onboarding.application import db as application_db
from loan_onboarding.application import schemas
from loan_onboarding.application.models import Application, ApplicationSubmissionResult
from loan_onboarding.customer import service as customer_service
from loan_onboarding.document import service as document_service
from loan_onboarding.workflow import service as workflow_service

# start_workflow()/handle.signal() only confirm Temporal *accepted* the
# call, not that persist_application/persist_resubmit has actually
# committed to Postgres yet (workflow/service.py's own docstrings). A
# caller (a BFF) immediately wants to show the created/resubmitted
# application, so poll our own table instead of trusting the signal
# alone -- same pattern, same constants, as
# review-approval-temporal's workflow/service.py's _wait_until().
_CONFIRM_TIMEOUT_S = 5.0
_CONFIRM_INTERVAL_S = 0.05

_temporal_client: Optional[Client] = None
_temporal_client_lock = asyncio.Lock()


async def _get_temporal_client() -> Client:
    global _temporal_client
    if _temporal_client is None:
        async with _temporal_client_lock:
            if _temporal_client is None:
                _temporal_client = await Client.connect(
                    os.environ.get("TEMPORAL_HOST", "localhost:7233"),
                    namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
                )
    return _temporal_client


async def _wait_until(
    application_id: UUID, predicate: Callable[[asyncpg.Record], bool]
) -> Optional[asyncpg.Record]:
    """Poll `application/db.py`'s own read until `predicate(record)` is
    true or we time out. Always returns whatever the last-read record
    was (or `None`), even on timeout -- callers should never fail a
    request just because the activity is running unusually slowly."""
    deadline = time.monotonic() + _CONFIRM_TIMEOUT_S
    while True:
        record = await application_db.get(application_id)
        if record is not None and predicate(record):
            return record
        if time.monotonic() >= deadline:
            return record
        await asyncio.sleep(_CONFIRM_INTERVAL_S)


async def create_application(
    applicant_identifier: str,
    product_type: str,
    payload: dict[str, Any],
    applicant_name: str,
    applicant_email: str,
    applicant_phone: str,
    amount: Decimal,
    application_id: Optional[UUID] = None,
) -> ApplicationSubmissionResult:
    """No `customer_id`/`account_id` params -- neither is guaranteed to
    exist yet (CLAUDE.md's "Applying without being a customer yet").

    `application_id` is optional -- see CLAUDE.md's note on this
    parameter (corrected from an earlier draft that gave this function
    no way to accept a pre-minted id, which conflicted with
    `bff_customer`'s document-upload-before-submit flow). Generates a
    fresh one if not given."""
    application_id = application_id or uuid.uuid4()

    validated_payload = schemas.validate_payload(product_type, payload)

    customer = await customer_service.find_by_identifier(applicant_identifier)
    customer_id = customer.customer_id if customer is not None else None

    missing = await document_service.check_completeness(str(application_id), product_type)
    if missing:
        return ApplicationSubmissionResult(
            application_id=application_id, application=None, missing_categories=missing
        )

    client = await _get_temporal_client()
    await workflow_service.start_workflow(
        client,
        str(application_id),
        product_type,
        validated_payload,
        float(amount),
        applicant_identifier,
        applicant_name,
        applicant_email,
        applicant_phone,
        str(customer_id) if customer_id is not None else None,
    )

    # start_workflow only confirms Temporal accepted the start -- wait
    # for persist_application (the workflow's first activity) to
    # actually insert the row before returning.
    record = await _wait_until(application_id, lambda r: True)
    application = Application.from_record(record) if record is not None else None
    return ApplicationSubmissionResult(application_id=application_id, application=application, missing_categories=[])
