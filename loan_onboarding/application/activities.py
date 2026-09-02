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
from uuid import UUID

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


@activity.defn
async def persist_application(inp: PersistApplicationInput) -> None:
    await application_db.insert(
        application_id=UUID(inp.application_id),
        applicant_identifier=inp.applicant_identifier,
        customer_id=UUID(inp.customer_id) if inp.customer_id else None,
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
async def persist_decision(inp: PersistDecisionInput) -> None:
    application_id = UUID(inp.application_id)
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

    customer_id: UUID | None = None
    account_id: UUID | None = None

    # Idempotency guard: a Temporal retry of an already-completed
    # APPROVED execution must not create a second account, a second
    # Welcome Letter, or re-promote an already-promoted Government ID --
    # `account_id IS NOT NULL` is proof this whole block already ran.
    #
    # **`account_id` must be persisted to Postgres immediately after
    # `create_account` succeeds, before the two `document.service` calls
    # below -- not deferred to the single write at the bottom.** Found by
    # actually running this against a real stack (P7-3): if either
    # `document.service` call raises (a real Mayan hiccup, not just a
    # theoretical one), Temporal retries this whole activity; without an
    # early write, the retry still sees `account_id IS NULL` and creates
    # a SECOND account for the same customer+product_type, hitting
    # `ux_accounts_customer_active_product_type`. Writing it here first
    # is what actually makes the guard above do what its own comment
    # already claimed. The tradeoff this accepts (already implied by
    # CLAUDE.md's "skip the entire provisioning block" wording, just not
    # correctly implemented before this fix): a retry that finds
    # `account_id` already set skips the `document.service` calls too,
    # even if one of them is what failed the first time -- a missing
    # Welcome Letter is a smaller, manually-recoverable gap than a
    # duplicated account, and matches this project's own
    # rare-enough-to-accept-for-a-POC stance elsewhere.
    if inp.resulting_status == "APPROVED" and record["account_id"] is None:
        if record["customer_id"] is not None:
            customer_id = record["customer_id"]
        else:
            customer = await customer_service.get_or_create(record["applicant_identifier"])
            customer_id = customer.customer_id

        account = await account_service.create_account(customer_id, record["product_type"])
        account_id = account.account_id

        await application_db.update_decision(
            application_id, status=inp.resulting_status, customer_id=customer_id, account_id=account_id
        )

        await document_service.promote_government_id_to_customer_photo(
            str(application_id), str(customer_id)
        )
        await document_service.generate_welcome_letter(
            str(account_id),
            str(customer_id),
            record["applicant_name"],
            record["product_type"],
            str(record["amount"]),
        )
    elif record["account_id"] is not None:
        # A retry that reaches here (resulting_status == "APPROVED", or
        # any other status if this activity is ever re-run for a
        # different reason) -- carry the already-resolved ids forward so
        # the final write below doesn't clobber them with NULL.
        customer_id = record["customer_id"]
        account_id = record["account_id"]

    await application_db.update_decision(
        application_id,
        status=inp.resulting_status,
        underwriter_name=underwriter_name,
        underwriter_comment=underwriter_comment,
        underwriter_decided_at=underwriter_decided_at,
        manager_name=manager_name,
        manager_comment=manager_comment,
        manager_decided_at=manager_decided_at,
        customer_id=customer_id,
        account_id=account_id,
        updated_at=inp.decided_at,
    )


@activity.defn
async def persist_resubmit(inp: PersistResubmitInput) -> None:
    await application_db.update_resubmission(UUID(inp.application_id), inp.payload)
