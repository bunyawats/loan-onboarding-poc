from __future__ import annotations

import asyncio
import os
import time
from typing import Callable, Optional

import asyncpg
from temporalio.client import Client

from loan_onboarding.account import db
from loan_onboarding.account.models import Account, AccountNotActive, AccountNotFound
from loan_onboarding.workflow import service as workflow_service
from loan_onboarding.workflow.task_queues import DEFAULT_TEMPORAL_HOST, DEFAULT_TEMPORAL_NAMESPACE

# Same "accepted != applied" polling pattern application/service.py's
# own _wait_until() already uses (see that module's docstring) --
# start_close_account_workflow only confirms Temporal accepted the
# start, not that persist_closure_request has actually committed.
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
                    os.environ.get("TEMPORAL_HOST", DEFAULT_TEMPORAL_HOST),
                    namespace=os.environ.get("TEMPORAL_NAMESPACE", DEFAULT_TEMPORAL_NAMESPACE),
                )
    return _temporal_client


async def _wait_until(
    account_id: str, predicate: Callable[[asyncpg.Record], bool]
) -> Optional[asyncpg.Record]:
    """Poll account/db.py's own read until predicate(record) is true or
    we time out. Always returns whatever the last-read record was (or
    None), even on timeout -- same contract
    application.service._wait_until already documents."""
    deadline = time.monotonic() + _CONFIRM_TIMEOUT_S
    while True:
        record = await db.get(account_id)
        if record is not None and predicate(record):
            return record
        if time.monotonic() >= deadline:
            return record
        await asyncio.sleep(_CONFIRM_INTERVAL_S)


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


async def request_closure(account_id: str, applicant_identifier: str) -> str:
    """Starts CloseAccountWorkflow -- only reachable while the account is
    currently ACTIVE (CLAUDE.md's "Account closure"); a stale UI or a
    direct call made after a request is already pending (or the account
    is already CLOSED) raises AccountNotActive instead of starting a
    second, colliding execution -- same "the UI hides it, the service
    still enforces it" discipline application.service's product-type
    picker already follows for its own hard elimination. This check is
    also what makes workflow.service._workflow_id_for_account_closure's
    deterministic `account-closure-<account_id>` id safe to reuse: it
    guarantees there's never a live CloseAccountWorkflow execution under
    that id when this function calls start_close_account_workflow.

    `applicant_identifier` is an opaque pass-through, not resolved here
    -- account/ still doesn't import customer/ (CLAUDE.md's module
    dependency graph: only workflow/ and notifications/ are this
    module's new exceptions, found necessary while building this very
    function -- accounts carries no applicant_identifier column of its
    own, only customer_id, and resolving customer_id -> applicant_identifier
    would need a customer.service.get(...) call this module isn't
    granted). The caller (bff_customer, P18-7) already holds this value
    from its own session cookie and threads it through so
    persist_closure_decision (account/activities.py) can eventually pass
    it to notifications.service.send_account_closure_decision."""
    account = await get(account_id)
    if account.status != "ACTIVE":
        raise AccountNotActive(
            f"account {account_id} is not ACTIVE (status={account.status!r}), cannot request closure"
        )

    client = await _get_temporal_client()
    workflow_id = await workflow_service.start_close_account_workflow(
        client, account_id, applicant_identifier
    )

    # start_close_account_workflow only confirms Temporal accepted the
    # start -- wait for persist_closure_request (the workflow's first
    # activity) to actually commit before returning.
    await _wait_until(account_id, lambda r: r["status"] == "CLOSURE_REQUESTED")
    return workflow_id


async def list_pending_closure_requests() -> list[Account]:
    """Read-only. Backs `bff_backoffice`'s closure-request queue screen
    (P18-6) — see `account/db.py`'s `list_by_status` for why this is
    deliberately unpaginated, unlike `application.service.list_by_status`'s
    own count-cache-backed queues."""
    records = await db.list_by_status("CLOSURE_REQUESTED")
    return [Account.from_record(r) for r in records]


async def wait_for_status_change(account_id: str, previous_status: str) -> Account:
    """Poll until `status` no longer equals `previous_status` or we time
    out — same bounded `_wait_until()` this module already uses for
    `request_closure`, and the same role
    `application.service.wait_for_status_change` plays for applications:
    `workflow.service.signal_close_account_decision`/
    `signal_close_account_cancel` only confirm Temporal *accepted* the
    signal, so a caller (`bff_backoffice`/`bff_customer`) that
    immediately wants to show the post-decision state needs this rather
    than trusting whatever was true a moment ago. Always returns the
    current `Account` even on timeout, same accepted imprecision
    `application.service`'s own version documents."""
    record = await _wait_until(account_id, lambda r: r["status"] != previous_status)
    assert record is not None, f"account {account_id} disappeared while waiting for a decision"
    return Account.from_record(record)
