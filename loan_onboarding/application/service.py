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
from loan_onboarding.application.models import (
    Application,
    ApplicationNotFound,
    ApplicationPage,
    ApplicationSubmissionResult,
)
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


async def resubmit_application(application_id: UUID, payload: dict[str, Any]) -> ApplicationSubmissionResult:
    """Same document gate re-check as `create_application`, then
    `workflow.service.signal_resubmit()` against the *existing*
    `workflow_id` -- the same running execution, still waiting from
    `MORE_INFO_REQUESTED`, never a new workflow start."""
    record = await application_db.get(application_id)
    if record is None:
        raise ApplicationNotFound(application_id)

    validated_payload = schemas.validate_payload(record["product_type"], payload)

    missing = await document_service.check_completeness(str(application_id), record["product_type"])
    if missing:
        return ApplicationSubmissionResult(
            application_id=application_id, application=None, missing_categories=missing
        )

    client = await _get_temporal_client()
    await workflow_service.signal_resubmit(client, record["workflow_id"], validated_payload)

    updated = await _wait_until(application_id, lambda r: r["payload"] == validated_payload)
    application = Application.from_record(updated) if updated is not None else None
    return ApplicationSubmissionResult(application_id=application_id, application=application, missing_categories=[])


async def check_decision_allowed(application_id: UUID, decision: str) -> list[str]:
    """Blocking-reason strings, `[]` if `decision` may proceed -- a
    no-op unless `decision == "APPROVE"` (PRD §9.2's
    one-active-account-per-product-type rule; Reject/RequestMoreInfo/
    Cancel never create an account, so never conflict). Called by
    `bff_backoffice` *before* signalling a decision -- never by
    `application/activities.py`, which has no clean way to surface an
    error back to a decision-maker from inside a running activity."""
    if decision != "APPROVE":
        return []

    record = await application_db.get(application_id)
    if record is None or record["customer_id"] is None:
        # A brand-new applicant (no resolved customer yet) can't
        # possibly conflict with an existing active account.
        return []

    has_active = await account_service.has_active_account_of_type(record["customer_id"], record["product_type"])
    if has_active:
        return [f"customer already has an active {record['product_type']} account"]
    return []


# ----------------------------------------------------------------------
# Paginated queries -- mint-once count cache, `list-pagination-bulk-
# actions` skill's Part 1 pattern. In-process only, deliberately (same
# reasoning review-approval-temporal's own workflow/service.py states
# for its identical cache): a query_id minted on one replica is just a
# cache miss on another, never a wrong answer, since every lookup path
# below degrades to a fresh COUNT(*) on a miss.
# ----------------------------------------------------------------------

QUERY_CACHE_TTL_S = 30.0
_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 100

# query_id -> (filter, total, expires_at)
_query_cache: dict[str, tuple[dict[str, str], int, float]] = {}


def _mint_query_id() -> str:
    return f"q_{uuid.uuid4().hex[:12]}"


def _cache_total(filter_key: dict[str, str], total: int) -> str:
    query_id = _mint_query_id()
    _query_cache[query_id] = (filter_key, total, time.monotonic() + QUERY_CACHE_TTL_S)
    return query_id


def _lookup_cached_total(query_id: Optional[str], filter_key: dict[str, str]) -> Optional[int]:
    if query_id is None:
        return None
    cached = _query_cache.get(query_id)
    if cached is None:
        return None
    cached_filter, total, expires_at = cached
    if time.monotonic() >= expires_at:
        del _query_cache[query_id]
        return None
    if cached_filter != filter_key:
        # Visibility-invariant defense (the skill's Part 1, point 4): a
        # query_id is only ever a shortcut for "the same query as last
        # time" -- never trust it for a different filter, even one from
        # the same caller a moment later. Recompute for real instead of
        # silently reusing another filter's count.
        return None
    return total


async def get(application_id: UUID) -> Application:
    record = await application_db.get(application_id)
    if record is None:
        raise ApplicationNotFound(application_id)
    return Application.from_record(record)


async def list_for_applicant(
    applicant_identifier: str,
    page: int = 1,
    page_size: int = _DEFAULT_PAGE_SIZE,
    query_id: Optional[str] = None,
) -> ApplicationPage:
    """Keyed on `applicant_identifier`, NOT `customer_id` -- has to work
    for an applicant with no approved application yet, whose
    `customer_id` is still `NULL` on every row (CLAUDE.md's "Applying
    without being a customer yet")."""
    page = max(page, 1)
    page_size = min(max(page_size, 1), _MAX_PAGE_SIZE)
    filter_key = {"applicant_identifier": applicant_identifier}

    total = _lookup_cached_total(query_id, filter_key)
    if total is None:
        total = await application_db.count_for_applicant(applicant_identifier)
        query_id = _cache_total(filter_key, total)

    offset = (page - 1) * page_size
    records = await application_db.list_for_applicant(applicant_identifier, page_size, offset)
    items = [Application.from_record(r) for r in records]
    return ApplicationPage(items=items, total=total, page=page, page_size=page_size, query_id=query_id)


async def list_by_status(
    status: str,
    page: int = 1,
    page_size: int = _DEFAULT_PAGE_SIZE,
    query_id: Optional[str] = None,
) -> ApplicationPage:
    """Staff queues (Underwriter/Manager, `bff_backoffice`)."""
    page = max(page, 1)
    page_size = min(max(page_size, 1), _MAX_PAGE_SIZE)
    filter_key = {"status": status}

    total = _lookup_cached_total(query_id, filter_key)
    if total is None:
        total = await application_db.count_by_status(status)
        query_id = _cache_total(filter_key, total)

    offset = (page - 1) * page_size
    records = await application_db.list_by_status(status, page_size, offset)
    items = [Application.from_record(r) for r in records]
    return ApplicationPage(items=items, total=total, page=page, page_size=page_size, query_id=query_id)


async def wait_for_status_change(application_id: UUID, previous_status: str) -> Application:
    """Poll until `status` no longer equals `previous_status` or we time
    out (same bounded `_wait_until()` this module already uses for
    `create_application`/`resubmit_application`) -- `bff_backoffice`
    (Phase 10) calls this right after `workflow.service.signal_decision()`/
    `bulk_signal_decision()`, which only confirm Temporal *accepted* the
    signal, so a route that immediately re-renders the affected row(s)
    needs this to show the actual post-decision state rather than
    whatever was true a moment ago. Always returns the current
    `Application` even on timeout (the activity is just running slower
    than usual -- the row still re-renders, just possibly still showing
    the pre-decision status until the next 5s poll catches up)."""
    record = await _wait_until(application_id, lambda r: r["status"] != previous_status)
    assert record is not None, f"application {application_id} disappeared while waiting for a decision"
    return Application.from_record(record)
