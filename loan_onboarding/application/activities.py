"""Concrete Temporal activity implementations, registered under the
exact string names `workflow/workflows.py` calls by name (CLAUDE.md's
"Breaking the cycle"). This is the one file in `application/` allowed
to import `customer/` and `account/` -- provisioning a customer/account
is what a terminal `APPROVED` decision actually does now (see
CLAUDE.md's "Applying without being a customer yet"). It already
imports `document/` for the submission-gate check's sibling calls, so
the two managed-document calls below aren't a new module-boundary edge.

Each of the three activities writes to `application/db.py` directly --
the one place in this module allowed to touch the `applications` table
-- and is kept separate rather than collapsed into one generic activity,
since each has different column-update semantics (same reasoning
`review-approval-temporal`'s own `activities.py` documents for its own
three)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import asyncpg
from temporalio import activity

from loan_onboarding.account import service as account_service
from loan_onboarding.application import db as application_db
from loan_onboarding.customer import service as customer_service
from loan_onboarding.document import service as document_service
from loan_onboarding.workflow.workflows import (
    PersistApplicationInput,
    PersistDecisionInput,
    PersistResubmitInput,
)

# The one business-rule constraint account/db.py's create() deliberately
# lets propagate uncontested (see its own docstring) -- the caller here
# is exactly the "someone who decides how to handle it" that comment
# refers to. Any other UniqueViolationError (e.g. an accounts_pkey
# collision) is already retried inside account/db.py itself and should
# never reach here; re-raise it unrecognized rather than mask it.
_ACTIVE_ACCOUNT_CONFLICT_CONSTRAINT = "ux_accounts_customer_active_product_type"


@activity.defn
async def persist_application(inp: PersistApplicationInput) -> None:
    await application_db.insert(
        application_id=inp.application_id,
        applicant_identifier=inp.applicant_identifier,
        customer_id=inp.customer_id,
        workflow_id=inp.workflow_id,
        product_type=inp.product_type,
        payload=inp.payload,
        applicant_name=inp.applicant_name,
        applicant_email=inp.applicant_email,
        applicant_phone=inp.applicant_phone,
        # inp.amount is a plain float over the wire (workflows.py's
        # ApplicationWorkflowInput) -- Decimal(str(...)) avoids the
        # binary-float precision artifacts a direct Decimal(float) would
        # introduce (e.g. Decimal(50000.1) != Decimal("50000.1")).
        amount=Decimal(str(inp.amount)),
    )


@activity.defn
async def persist_decision(inp: PersistDecisionInput) -> str:
    """Returns the status actually written -- normally `inp.resulting_status`
    verbatim, but see the active-account-conflict handling below, where
    it can differ. `workflows.py`'s `submit_decision` uses this return
    value (not its own pre-computed `resulting_status`) as the
    workflow's own `self._status`, so the two never disagree."""
    application_id = inp.application_id
    record = await application_db.get(application_id)
    assert record is not None, f"application {application_id} not found"

    # inp.decided_at is only ever set for the native-Temporal-cancel
    # path (workflows.py's except asyncio.CancelledError branch) -- every
    # normal signal-driven decision uses "now" instead, same as
    # review-approval-temporal's own persist_decision (it doesn't try to
    # preserve an exact original timestamp across a Temporal retry
    # either; recomputing "now" on each execution is an accepted, small
    # imprecision, not a correctness bug).
    decided_at = inp.decided_at or datetime.now(timezone.utc)

    underwriter_name = underwriter_comment = underwriter_decided_at = None
    manager_name = manager_comment = manager_decided_at = None
    if inp.actor_role == "underwriter":
        underwriter_name = inp.actor_name
        underwriter_comment = inp.comment
        underwriter_decided_at = decided_at
    elif inp.actor_role == "manager":
        manager_name = inp.actor_name
        manager_comment = inp.comment
        manager_decided_at = decided_at
    # actor_role == "customer" (CANCELLED, by the applicant or forced by
    # a native Temporal cancel) touches neither column set.

    customer_id: str | None = None
    final_status = inp.resulting_status

    # Idempotency guard: a Temporal retry of an already-completed
    # APPROVED execution must not create a second account, a second
    # Welcome Letter, or re-promote an already-promoted Government ID.
    # account_service.get_by_application_id(application_id) is proof
    # this whole block already ran -- the committed accounts row (its
    # application_id column is NOT NULL UNIQUE) is now the durable
    # idempotency marker on its own; there's no intermediate write back
    # onto applications the way an account_id column on this table used
    # to require.
    existing_account = None
    if inp.resulting_status == "APPROVED":
        existing_account = await account_service.get_by_application_id(application_id)

    if inp.resulting_status == "APPROVED" and existing_account is None:
        if record["customer_id"] is not None:
            customer_id = record["customer_id"]
        else:
            customer = await customer_service.get_or_create(record["applicant_identifier"])
            customer_id = customer.customer_id

        try:
            account = await account_service.create_account(customer_id, record["product_type"], application_id)
        except asyncpg.exceptions.UniqueViolationError as exc:
            if exc.constraint_name != _ACTIVE_ACCOUNT_CONFLICT_CONSTRAINT:
                raise
            # Lost the active-account-per-product-type race (CLAUDE.md's
            # Known Gaps): check_decision_allowed passed for this
            # application before it was signaled, but a concurrently-
            # processed application for the same customer+product_type
            # won the race to actually create the account -- confirmed
            # live via Phase 13's own bulk-approve verification. The
            # database constraint already stopped the bad state from
            # ever being written; converting this Approve into a clean
            # REJECTED outcome (rather than letting the exception
            # propagate) is what actually fixes the "stuck forever with
            # a FAILED Temporal workflow, no error surfaced to staff"
            # half of that gap. customer_id stays whatever it resolved
            # to above -- get_or_create is itself idempotent, so a
            # real, unused customer row isn't an orphaned-state concern.
            final_status = "REJECTED"
            conflict_note = (
                f"Automatically rejected: customer already has an active "
                f"{record['product_type']} account (lost a race against a "
                f"concurrently-processed application for the same customer)."
            )
            if inp.actor_role == "underwriter":
                underwriter_comment = f"{inp.comment} {conflict_note}".strip()
            elif inp.actor_role == "manager":
                manager_comment = f"{inp.comment} {conflict_note}".strip()
        else:
            await document_service.promote_government_id_to_customer_photo(application_id, customer_id)
            await document_service.generate_welcome_letter(
                account.account_id,
                customer_id,
                record["applicant_name"],
                record["product_type"],
                str(record["amount"]),
            )
    elif existing_account is not None:
        # A retry that finds the account already provisioned -- carry
        # the already-resolved customer_id forward so the final write
        # below doesn't clobber it with NULL. Both document.service
        # calls above are skipped entirely, permanently -- see
        # CLAUDE.md's "Applying without being a customer yet" for the
        # accepted tradeoff (a missing Welcome Letter is smaller and
        # more recoverable than a duplicated account).
        customer_id = existing_account.customer_id
    elif record["customer_id"] is not None:
        # Any other status (or a non-APPROVED re-run) -- carry the
        # already-resolved customer_id forward, same reasoning.
        customer_id = record["customer_id"]

    await application_db.update_decision(
        application_id,
        status=final_status,
        underwriter_name=underwriter_name,
        underwriter_comment=underwriter_comment,
        underwriter_decided_at=underwriter_decided_at,
        manager_name=manager_name,
        manager_comment=manager_comment,
        manager_decided_at=manager_decided_at,
        customer_id=customer_id,
        updated_at=inp.decided_at,
    )
    return final_status


@activity.defn
async def persist_resubmit(inp: PersistResubmitInput) -> None:
    await application_db.update_resubmission(inp.application_id, inp.payload)
