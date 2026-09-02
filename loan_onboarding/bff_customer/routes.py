"""`bff_customer`'s `/apply/*` routes -- the public-facing, mobile-first
self-service flow (PRD §8.1, CLAUDE.md's `bff_customer/` module
section). No business logic or data of its own -- pure orchestration +
presentation, calling straight into the domain modules' `service.py`
functions, same principle `bff_backoffice/routes.py` already follows.

No Keycloak here at all (CLAUDE.md's "Identity": customer side is a
signed cookie holding `applicant_identifier`, nothing more -- see
`identity.py`). Unlike the staff screens, this flow is mostly plain
`<form>` POST-redirect-GET navigation between full pages rather than
htmx fragment swaps -- a multi-page wizard suits a mobile step flow (and
its own back button) better than an SPA-style single page, and nothing
here needs auto-refresh or bulk selection the way the staff queues do.
htmx is used in exactly one place: the document-upload widget, so adding
a file doesn't reload the whole page and lose the customer's place in a
multi-category upload screen.
"""

from __future__ import annotations

import os
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Optional
from uuid import UUID

import pydantic
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from temporalio.client import Client

from loan_onboarding.application import schemas as application_schemas
from loan_onboarding.application import service as application_service
from loan_onboarding.application.models import Application, ApplicationNotFound
from loan_onboarding.bff_customer import identity
from loan_onboarding.document import service as document_service
from loan_onboarding.document.models import UploadedFile
from loan_onboarding.workflow import service as workflow_service
from loan_onboarding.workflow.workflows import TERMINAL_STATUSES

router = APIRouter(prefix="/apply", tags=["Customer"])
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

_PAGE_SIZE = 10
_DRAFT_KEY = "draft_application"

PRODUCT_LABELS = {
    "personal_loan": "Personal Loan",
    "auto_loan": "Auto Loan",
    "mortgage": "Mortgage",
}

# UI-only mirror of PRD §6.1's product-specific field table -- the
# actual source of truth for what's *valid* is
# application.schemas.PRODUCT_TYPE_SCHEMAS (a Pydantic model per product
# type), which `_validate_product_fields` below always runs against
# before anything is stored. This dict exists purely to render the right
# input widgets in the right order; same "duplicated on purpose, no
# import-time link" reasoning document/service.py's own
# REQUIRED_CATEGORIES dict documents for the analogous case.
PRODUCT_FIELDS: dict[str, list[tuple[str, str, str]]] = {
    "personal_loan": [
        ("purpose", "Purpose", "text"),
        ("employment_status", "Employment Status", "text"),
        ("monthly_income", "Monthly Income (USD)", "number"),
    ],
    "auto_loan": [
        ("vehicle_make_model", "Vehicle Make / Model", "text"),
        ("vin", "VIN", "text"),
        ("down_payment", "Down Payment (USD)", "number"),
    ],
    "mortgage": [
        ("property_address", "Property Address", "text"),
        ("appraised_value", "Appraised Value (USD)", "number"),
        ("down_payment", "Down Payment (USD)", "number"),
    ],
}

STATUS_LABELS = {
    "PENDING_UNDERWRITING": "Under Review",
    "PENDING_MANAGER_APPROVAL": "Escalated for Manager Review",
    "MORE_INFO_REQUESTED": "More Info Requested",
    "APPROVED": "Approved",
    "REJECTED": "Rejected",
    "CANCELLED": "Cancelled",
}


def _category_slug(category: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-")


templates.env.globals["category_slug"] = _category_slug
templates.env.globals["status_label"] = lambda status: STATUS_LABELS.get(status, status)
templates.env.globals["product_label"] = lambda pt: PRODUCT_LABELS.get(pt, pt)


def _build_timeline(application: Application) -> list[dict[str, str]]:
    """PRD §8.1's "simple timeline (submitted -> under review ->
    [escalated] -> decision)". Checks the terminal case FIRST, not last
    -- a Cancel can happen from any non-terminal state (PENDING_UNDERWRITING,
    PENDING_MANAGER_APPROVAL, or MORE_INFO_REQUESTED), so branching on
    `status` in wizard order would miss it whenever cancellation didn't
    happen from the last step."""
    steps = [{"label": "Submitted", "state": "done"}]
    if application.status in TERMINAL_STATUSES:
        steps.append({"label": "Under Review", "state": "done"})
        if application.manager_decided_at is not None:
            steps.append({"label": "Escalated to Manager", "state": "done"})
        steps.append({"label": STATUS_LABELS[application.status], "state": "current"})
        return steps
    if application.status == "PENDING_UNDERWRITING":
        steps.append({"label": "Under Review", "state": "current"})
        return steps
    if application.status == "PENDING_MANAGER_APPROVAL":
        steps.append({"label": "Under Review", "state": "done"})
        steps.append({"label": "Escalated to Manager", "state": "current"})
        return steps
    if application.status == "MORE_INFO_REQUESTED":
        steps.append({"label": "Under Review", "state": "done"})
        steps.append({"label": "More Info Requested", "state": "current"})
        return steps
    return steps


# ---------------------------------------------------------------- identity ----


class IdentifyRequired(Exception):
    """Raised by `_require_applicant` when no session cookie is set yet
    -- `app.py` maps this to a 303 redirect to `/apply/identify`, same
    "exception -> route-agnostic redirect" shape `bff_backoffice`'s
    `RequireLoginRedirect` already uses for its own login gate."""


async def _require_applicant(request: Request) -> str:
    identifier = identity.get_applicant_identifier(request)
    if identifier is None:
        raise IdentifyRequired()
    return identifier


@router.get("/", response_class=RedirectResponse)
async def apply_root(request: Request):
    if identity.get_applicant_identifier(request) is None:
        return RedirectResponse(url="/apply/identify", status_code=303)
    return RedirectResponse(url="/apply/applications", status_code=303)


@router.get("/identify", response_class=HTMLResponse)
async def identify_form(request: Request):
    if identity.get_applicant_identifier(request) is not None:
        return RedirectResponse(url="/apply/applications", status_code=303)
    return templates.TemplateResponse(request, "identify.html", {})


@router.post("/identify", response_class=HTMLResponse)
async def identify_submit(request: Request, applicant_identifier: str = Form(...)):
    applicant_identifier = applicant_identifier.strip()
    if not applicant_identifier:
        return templates.TemplateResponse(
            request, "identify.html", {"error": "Enter an email or phone number."}, status_code=400
        )
    response = RedirectResponse(url="/apply/applications", status_code=303)
    identity.set_applicant_identifier(response, applicant_identifier)
    return response


@router.post("/switch-identity", response_class=RedirectResponse)
async def switch_identity(request: Request):
    response = RedirectResponse(url="/apply/identify", status_code=303)
    identity.clear_applicant_identifier(response)
    # The wizard draft (if any) belongs to the identity being switched
    # away from -- drop it too, so a half-finished application never
    # leaks into the next identity's session.
    request.session.pop(_DRAFT_KEY, None)
    return response


# ------------------------------------------------------------- applications ----


@router.get("/applications", response_class=HTMLResponse)
async def my_applications(
    request: Request, page: int = 1, applicant_identifier: str = Depends(_require_applicant)
):
    result = await application_service.list_for_applicant(applicant_identifier, page=page, page_size=_PAGE_SIZE)
    total_pages = max(1, (result.total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    return templates.TemplateResponse(
        request,
        "applications_list.html",
        {
            "applicant_identifier": applicant_identifier,
            "applications": result.items,
            "page": result.page,
            "total_pages": total_pages,
        },
    )


async def _owned_application(application_id: UUID, applicant_identifier: str) -> Application:
    """The customer-facing visibility invariant (PRD §7.1, §10 success
    criterion 2): filters by `applicant_identifier`, not by whether the
    id merely parses -- a customer who guesses another applicant's
    `application_id` gets a 404, identical to a nonexistent id, never a
    real record."""
    try:
        application = await application_service.get(application_id)
    except ApplicationNotFound:
        raise HTTPException(status_code=404)
    if application.applicant_identifier != applicant_identifier:
        raise HTTPException(status_code=404)
    return application


async def _detail_context(application: Application) -> dict[str, Any]:
    """Shared context builder for the detail page's initial GET and
    every resubmit-error branch that has to re-render the same page --
    keeps the "documents grouped by category" and "which categories can
    still take an upload" logic in one place rather than copy-pasted at
    each call site."""
    application_id = application.application_id
    documents = await document_service.list_documents(str(application_id))
    required = document_service.REQUIRED_CATEGORIES[application.product_type]
    by_category = {category: [d for d in documents if d.category == category] for category in required}
    categories = None
    if application.status == "MORE_INFO_REQUESTED":
        # Additional documents may only be added while more info is
        # requested (PRD §8.1) -- the upload-capable rendering
        # (`_document_category.html`'s <form>) is only built in that
        # state; every other status gets a plain read-only `by_category`.
        categories = [
            {
                "category": category,
                "documents": by_category[category],
                "upload_url": f"/apply/applications/{application_id}/documents/upload",
                "capture": category == "Government ID",
            }
            for category in required
        ]
    return {
        "application": application,
        "timeline": _build_timeline(application),
        "product_fields": PRODUCT_FIELDS[application.product_type],
        "by_category": by_category,
        "categories": categories,
        "is_terminal": application.status in TERMINAL_STATUSES,
    }


@router.get("/applications/{application_id}", response_class=HTMLResponse)
async def application_detail(
    request: Request, application_id: UUID, applicant_identifier: str = Depends(_require_applicant)
):
    application = await _owned_application(application_id, applicant_identifier)
    return templates.TemplateResponse(request, "application_detail.html", await _detail_context(application))


@router.post("/applications/{application_id}/cancel", response_class=RedirectResponse)
async def cancel_application(
    request: Request, application_id: UUID, applicant_identifier: str = Depends(_require_applicant)
):
    application = await _owned_application(application_id, applicant_identifier)
    if application.status in TERMINAL_STATUSES:
        raise HTTPException(status_code=400, detail="application is already in a terminal state")
    client = await _get_temporal_client()
    await workflow_service.signal_decision(
        client, application.workflow_id, "customer", "CANCELLED", applicant_identifier, ""
    )
    await application_service.wait_for_status_change(application_id, application.status)
    return RedirectResponse(url=f"/apply/applications/{application_id}", status_code=303)


@router.post("/applications/{application_id}/resubmit", response_class=HTMLResponse)
async def resubmit_application(
    request: Request, application_id: UUID, applicant_identifier: str = Depends(_require_applicant)
):
    application = await _owned_application(application_id, applicant_identifier)
    if application.status != "MORE_INFO_REQUESTED":
        raise HTTPException(status_code=400, detail="application is not awaiting more info")

    form = await request.form()
    field_names = [name for name, _, _ in PRODUCT_FIELDS[application.product_type]]
    fields = {name: str(form.get(name, "")).strip() for name in field_names}

    errors = _validate_product_fields(application.product_type, fields)
    if errors:
        context = await _detail_context(application)
        context.update(resubmit_errors=errors, resubmit_values=fields)
        return templates.TemplateResponse(request, "application_detail.html", context, status_code=400)

    result = await application_service.resubmit_application(application_id, fields)
    if result.missing_categories:
        context = await _detail_context(application)
        context.update(
            resubmit_error="Missing required documents: " + ", ".join(result.missing_categories),
            resubmit_values=fields,
        )
        return templates.TemplateResponse(request, "application_detail.html", context, status_code=400)

    return RedirectResponse(url=f"/apply/applications/{application_id}", status_code=303)


@router.post("/applications/{application_id}/documents/upload", response_class=HTMLResponse)
async def upload_more_info_document(
    request: Request,
    application_id: UUID,
    category: str = Form(...),
    file: UploadFile = File(...),
    applicant_identifier: str = Depends(_require_applicant),
):
    application = await _owned_application(application_id, applicant_identifier)
    if application.status != "MORE_INFO_REQUESTED":
        raise HTTPException(status_code=400, detail="documents can only be added while more info is requested")
    required = document_service.REQUIRED_CATEGORIES[application.product_type]
    if category not in required:
        raise HTTPException(status_code=400, detail=f"unknown category {category!r} for this product type")

    content = await file.read()
    await document_service.upload(applicant_identifier, str(application_id), category, UploadedFile(file.filename, content))

    documents = await document_service.list_documents(str(application_id))
    category_documents = [d for d in documents if d.category == category]
    return templates.TemplateResponse(
        request,
        "_document_category.html",
        {
            "category": category,
            "documents": category_documents,
            "upload_url": f"/apply/applications/{application_id}/documents/upload",
        },
    )


async def _document_preview(application_id: UUID, document_id: int) -> StreamingResponse:
    try:
        stream = await document_service.preview(str(application_id), document_id)
    except document_service.DocumentNotFound:
        raise HTTPException(status_code=404)

    async def _iter_and_close():
        try:
            async for chunk in stream.aiter_bytes():
                yield chunk
        finally:
            await stream.aclose()

    return StreamingResponse(
        _iter_and_close(),
        media_type=stream.content_type,
        headers={"Content-Disposition": f'inline; filename="{stream.filename}"'},
    )


@router.get("/applications/{application_id}/documents/{document_id}/preview")
async def application_document_preview(
    application_id: UUID, document_id: int, applicant_identifier: str = Depends(_require_applicant)
):
    await _owned_application(application_id, applicant_identifier)
    return await _document_preview(application_id, document_id)


# --------------------------------------------------------- new application ----


def _validate_product_fields(product_type: str, fields: dict[str, str]) -> dict[str, str]:
    """Dry-run `application.schemas.validate_payload` so field-level
    errors can be shown right on the wizard's own form, instead of
    surfacing as a generic failure only at final submit (PRD §6.4's "a
    missing category surfaces as a clear... message, not a silent
    failure" applies just as much to a malformed field). Returns a
    `{field_name: message}` map, `{}` if valid. Field-name attribution is
    best-effort (pydantic errors report a `loc` tuple whose first
    element is the field name for these flat models)."""
    try:
        application_schemas.validate_payload(product_type, fields)
    except pydantic.ValidationError as exc:
        errors: dict[str, str] = {}
        for error in exc.errors():
            field = str(error["loc"][0]) if error["loc"] else "__all__"
            errors[field] = error["msg"]
        return errors
    return {}


@router.get("/new", response_class=HTMLResponse)
async def new_application_picker(request: Request, applicant_identifier: str = Depends(_require_applicant)):
    return templates.TemplateResponse(request, "new_product_picker.html", {"products": PRODUCT_LABELS})


@router.post("/new/start", response_class=RedirectResponse)
async def new_application_start(
    request: Request, product_type: str = Form(...), applicant_identifier: str = Depends(_require_applicant)
):
    if product_type not in PRODUCT_FIELDS:
        raise HTTPException(status_code=400, detail=f"unknown product_type {product_type!r}")
    request.session[_DRAFT_KEY] = {
        "product_type": product_type,
        "application_id": str(uuid.uuid4()),
        "fields": {},
    }
    return RedirectResponse(url="/apply/new/details", status_code=303)


@router.get("/new/details", response_class=HTMLResponse)
async def new_application_details(request: Request, applicant_identifier: str = Depends(_require_applicant)):
    draft = request.session.get(_DRAFT_KEY)
    if draft is None:
        return RedirectResponse(url="/apply/new", status_code=303)
    return templates.TemplateResponse(
        request,
        "new_details.html",
        {
            "product_type": draft["product_type"],
            "product_fields": PRODUCT_FIELDS[draft["product_type"]],
            "values": draft["fields"],
            "errors": {},
        },
    )


@router.post("/new/details", response_class=HTMLResponse)
async def new_application_details_submit(request: Request, applicant_identifier: str = Depends(_require_applicant)):
    draft = request.session.get(_DRAFT_KEY)
    if draft is None:
        return RedirectResponse(url="/apply/new", status_code=303)
    product_type = draft["product_type"]

    form = await request.form()
    common_fields = ["applicant_name", "applicant_email", "applicant_phone", "amount"]
    product_field_names = [name for name, _, _ in PRODUCT_FIELDS[product_type]]
    values = {name: str(form.get(name, "")).strip() for name in common_fields + product_field_names}

    errors: dict[str, str] = {}
    for name in ("applicant_name", "applicant_email", "applicant_phone"):
        if not values[name]:
            errors[name] = "required"
    try:
        if Decimal(values["amount"]) <= 0:
            errors["amount"] = "must be a positive number"
    except (InvalidOperation, KeyError):
        errors["amount"] = "must be a number"

    product_fields = {name: values[name] for name in product_field_names}
    errors.update(_validate_product_fields(product_type, product_fields))

    if errors:
        return templates.TemplateResponse(
            request,
            "new_details.html",
            {
                "product_type": product_type,
                "product_fields": PRODUCT_FIELDS[product_type],
                "values": values,
                "errors": errors,
            },
            status_code=400,
        )

    draft["fields"] = values
    request.session[_DRAFT_KEY] = draft
    return RedirectResponse(url="/apply/new/documents", status_code=303)


@router.get("/new/documents", response_class=HTMLResponse)
async def new_application_documents(request: Request, applicant_identifier: str = Depends(_require_applicant)):
    draft = request.session.get(_DRAFT_KEY)
    if draft is None:
        return RedirectResponse(url="/apply/new", status_code=303)
    application_id = draft["application_id"]
    product_type = draft["product_type"]

    documents = await document_service.list_documents(application_id)
    required = document_service.REQUIRED_CATEGORIES[product_type]
    categories = [
        {
            "category": category,
            "documents": [d for d in documents if d.category == category],
            "upload_url": "/apply/new/documents/upload",
            # Camera-capture hint (PRD §6.4/§8.1) only makes sense for a
            # physical ID card, not a multi-page bank statement/report.
            "capture": category == "Government ID",
        }
        for category in required
    ]
    return templates.TemplateResponse(request, "new_documents.html", {"categories": categories})


@router.post("/new/documents/upload", response_class=HTMLResponse)
async def new_application_upload(
    request: Request,
    category: str = Form(...),
    file: UploadFile = File(...),
    applicant_identifier: str = Depends(_require_applicant),
):
    draft = request.session.get(_DRAFT_KEY)
    if draft is None:
        raise HTTPException(status_code=400, detail="no application in progress")
    product_type = draft["product_type"]
    required = document_service.REQUIRED_CATEGORIES[product_type]
    if category not in required:
        raise HTTPException(status_code=400, detail=f"unknown category {category!r} for this product type")

    content = await file.read()
    await document_service.upload(applicant_identifier, draft["application_id"], category, UploadedFile(file.filename, content))

    documents = await document_service.list_documents(draft["application_id"])
    category_documents = [d for d in documents if d.category == category]
    return templates.TemplateResponse(
        request,
        "_document_category.html",
        {"category": category, "documents": category_documents, "upload_url": "/apply/new/documents/upload"},
    )


@router.get("/new/review", response_class=HTMLResponse)
async def new_application_review(request: Request, applicant_identifier: str = Depends(_require_applicant)):
    draft = request.session.get(_DRAFT_KEY)
    if draft is None:
        return RedirectResponse(url="/apply/new", status_code=303)
    product_type = draft["product_type"]
    application_id = draft["application_id"]

    documents = await document_service.list_documents(application_id)
    required = document_service.REQUIRED_CATEGORIES[product_type]
    by_category = {category: [d for d in documents if d.category == category] for category in required}
    return templates.TemplateResponse(
        request,
        "new_review.html",
        {
            "product_type": product_type,
            "product_fields": PRODUCT_FIELDS[product_type],
            "values": draft["fields"],
            "by_category": by_category,
        },
    )


@router.post("/new/review", response_class=HTMLResponse)
async def new_application_submit(request: Request, applicant_identifier: str = Depends(_require_applicant)):
    draft = request.session.get(_DRAFT_KEY)
    if draft is None:
        return RedirectResponse(url="/apply/new", status_code=303)
    product_type = draft["product_type"]
    application_id = UUID(draft["application_id"])
    values = draft["fields"]
    product_field_names = [name for name, _, _ in PRODUCT_FIELDS[product_type]]
    payload = {name: values[name] for name in product_field_names}

    result = await application_service.create_application(
        applicant_identifier=applicant_identifier,
        product_type=product_type,
        payload=payload,
        applicant_name=values["applicant_name"],
        applicant_email=values["applicant_email"],
        applicant_phone=values["applicant_phone"],
        amount=Decimal(values["amount"]),
        application_id=application_id,
    )

    if result.missing_categories:
        documents = await document_service.list_documents(draft["application_id"])
        required = document_service.REQUIRED_CATEGORIES[product_type]
        by_category = {category: [d for d in documents if d.category == category] for category in required}
        return templates.TemplateResponse(
            request,
            "new_review.html",
            {
                "product_type": product_type,
                "product_fields": PRODUCT_FIELDS[product_type],
                "values": values,
                "by_category": by_category,
                "error": "Missing required documents: " + ", ".join(result.missing_categories),
            },
            status_code=400,
        )

    request.session.pop(_DRAFT_KEY, None)
    return RedirectResponse(url=f"/apply/applications/{result.application_id}", status_code=303)


# ---------------------------------------------------------- Temporal client ----

_temporal_client: Optional[Client] = None


async def _get_temporal_client() -> Client:
    # Same lazy-singleton-per-process pattern application/service.py and
    # bff_backoffice/routes.py both already use -- workflow.service's
    # functions take `client` as a plain parameter, so every entry point
    # that calls them owns its own connection.
    global _temporal_client
    if _temporal_client is None:
        _temporal_client = await Client.connect(
            os.environ.get("TEMPORAL_HOST", "localhost:7233"),
            namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        )
    return _temporal_client
