"""Registry of product-specific payload schemas (PRD §6.1). Payload
fields here are only what's specific to a product -- amount, applicant
name/email/phone are common fields captured as their own columns
(`db/schema.sql`), never folded into `payload` (CLAUDE.md's
"Denormalized applicant fields, on purpose" and `workflow/`'s own
`amount`-as-named-argument reasoning).

Adding a new product type touches TWO places, not one:
  1. This file -- add a Pydantic model and a `PRODUCT_TYPE_SCHEMAS` entry.
  2. `workflow/task_queues.py` -- add the type string to
     `KNOWN_PRODUCT_TYPES`.
The assert below fails loudly at import time if these drift apart,
rather than silently starting a workflow on a task queue nothing polls
-- the payoff of being one process again, see CLAUDE.md's "Breaking the
cycle". This works only because `application/` is allowed to import
`workflow/` (a normal downward dependency, not a cycle) -- `document/`,
by contrast, can never do this check (see `document/service.py`'s own
`REQUIRED_CATEGORIES`, which duplicates the three product-type strings
with no import-time assert, precisely because it's a leaf module that
must never import `workflow/`)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from loan_onboarding.workflow.task_queues import KNOWN_PRODUCT_TYPES


class PersonalLoanPayload(BaseModel):
    purpose: str
    employment_status: str
    monthly_income: Decimal


class AutoLoanPayload(BaseModel):
    vehicle_make_model: str
    vin: str
    down_payment: Decimal


class MortgagePayload(BaseModel):
    property_address: str
    appraised_value: Decimal
    down_payment: Decimal


PRODUCT_TYPE_SCHEMAS: dict[str, type[BaseModel]] = {
    "personal_loan": PersonalLoanPayload,
    "auto_loan": AutoLoanPayload,
    "mortgage": MortgagePayload,
}

assert set(PRODUCT_TYPE_SCHEMAS) == set(KNOWN_PRODUCT_TYPES), (
    f"PRODUCT_TYPE_SCHEMAS {set(PRODUCT_TYPE_SCHEMAS)} and KNOWN_PRODUCT_TYPES "
    f"{set(KNOWN_PRODUCT_TYPES)} have drifted apart -- update both when "
    f"adding or removing a product type."
)


class UnknownProductType(ValueError):
    pass


def validate_payload(product_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    schema = PRODUCT_TYPE_SCHEMAS.get(product_type)
    if schema is None:
        raise UnknownProductType(
            f"unknown product_type {product_type!r}. Known types: {list(PRODUCT_TYPE_SCHEMAS)}"
        )
    return schema(**payload).model_dump(mode="json")
