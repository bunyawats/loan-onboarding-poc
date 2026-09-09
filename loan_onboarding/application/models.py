from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal
from typing import Any

import asyncpg


@dataclass(frozen=True, slots=True)
class Application:
    """The real loan application (`application_id`/`applicant_identifier`/
    `customer_id`/`product_type`/`payload`/`applicant_*`/`amount`/
    `status`/`created_at`) plus onboarding-workflow tracking, joined in
    from a separate table (Phase 23, "Split loan application request
    data (1:1)" -- see CLAUDE.md / IMPLEMENTATION_PLAN.md).

    `status` is the one field guaranteed present regardless: it's
    mirrored onto `applications` itself, so it survives even if
    `loan_apply_requests` (the table every field below `status` in this
    list actually lives on) is ever truncated. Every other field here
    --- `workflow_id`, `underwriter_*`, `manager_*`, `updated_at` --- is
    `None` in that degraded case (a `LEFT JOIN`, not a missing row --
    see `application/db.py`'s own `_SELECT_JOINED`), not just "possibly
    unset yet" the way they already could be pre-decision. `risk_tier`
    is deliberately not a field here at all -- write-only via
    `application/db.py`'s `update_decision`, never read back through
    this model, unaffected by this split."""

    application_id: str
    applicant_identifier: str
    customer_id: str | None
    workflow_id: str | None
    product_type: str
    payload: dict[str, Any]
    applicant_name: str
    applicant_email: str
    applicant_phone: str
    amount: Decimal
    status: str
    underwriter_name: str | None
    underwriter_comment: str | None
    underwriter_decided_at: datetime | None
    manager_name: str | None
    manager_comment: str | None
    manager_decided_at: datetime | None
    created_at: datetime
    # Optional (Phase 23) -- this column now lives only on
    # loan_apply_requests, so it's None whenever that row is missing
    # (truncated), same as workflow_id/underwriter_*/manager_* above.
    # Was NOT NULL pre-Phase-23; every other moved field was already
    # Optional and is unaffected by this change.
    updated_at: datetime | None

    @classmethod
    def from_record(cls, record: asyncpg.Record) -> "Application":
        return cls(**{f.name: record[f.name] for f in fields(cls)})


class ApplicationNotFound(Exception):
    """Raised by service.get() when no application exists for the given id."""


@dataclass(frozen=True, slots=True)
class ApplicationSubmissionResult:
    """Returned by both `create_application` and `resubmit_application`
    -- either carries the resulting `Application` (the document gate was
    satisfied and the workflow was started/signalled) or a non-empty
    `missing_categories` list (nothing was started). `application_id` is
    always present, even in the missing-categories branch (which
    persists no row) -- see CLAUDE.md's note on `create_application`'s
    optional `application_id` parameter: a caller that didn't pre-mint
    one can still learn what id its just-checked documents should be
    tagged under, then retry once they're uploaded."""

    application_id: str
    application: Application | None
    missing_categories: list[str]


@dataclass(frozen=True, slots=True)
class ApplicationPage:
    """One page of `list_for_applicant`/`list_by_status` -- `query_id`
    echoes back a mint-once count cache key (the `list-pagination-bulk-
    actions` skill's Part 1 pattern) so a caller paging through the same
    filter, or polling it, can skip a fresh `COUNT(*)`."""

    items: list[Application]
    total: int
    page: int
    page_size: int
    query_id: str
