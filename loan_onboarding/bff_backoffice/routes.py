"""
`bff_backoffice`'s `/ui/*` HTMX routes -- the internal-facing staff UI
("LOS"), Keycloak-gated (CLAUDE.md's "Identity"). Calls straight into
the domain modules' `service.py` functions -- no business logic here,
pure orchestration + presentation (CLAUDE.md's `bff_backoffice/` module
section).

Two authorization dependencies, for two different purposes -- see
`keycloak_session.py`'s module docstring for the full reasoning:
`_role_dependency(role)` gates page/screen selection (which list a
staff member sees); `_permission_dependency(permission)` gates the five
mutating actions via a real Keycloak permission check. **No
`require_session_role` pre-gate on any decision route** (CLAUDE.md's
explicit warning, itself citing a past incident in the reference
project this is adapted from) -- decision routes use the plain
`_session_user_dependency` (any valid session) plus an explicit
`check_permission()` call, since which permission is required depends
on the submitted decision.

Underwriter and Manager screens share one set of route handlers,
parameterized by `role` (`"underwriter"`/`"manager"`) and one shared
template set -- a deliberate simplification versus the reference
project's literal per-role file duplication, reasonable here since the
two screens differ only in which `status` they filter on and which
decisions/permissions apply, not in structure.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from temporalio.client import Client

from loan_onboarding.account import service as account_service
from loan_onboarding.application import service as application_service
from loan_onboarding.application.models import ApplicationNotFound, ApplicationPage
from loan_onboarding.bff_backoffice import keycloak_auth, keycloak_session, selection_store
from loan_onboarding.bff_backoffice.keycloak_session import SESSION_KEY
from loan_onboarding.customer import service as customer_service
from loan_onboarding.document import service as document_service
from loan_onboarding.workflow import service as workflow_service
from loan_onboarding.workflow.task_queues import DEFAULT_TEMPORAL_HOST, DEFAULT_TEMPORAL_NAMESPACE
from loan_onboarding.workflow.workflows import (
    DECISION_APPROVE,
    DECISION_REJECT,
    DECISION_REQUEST_MORE_INFO,
    ROLE_MANAGER,
    ROLE_UNDERWRITER,
    STATUS_PENDING_MANAGER_APPROVAL,
    STATUS_PENDING_UNDERWRITING,
)

router = APIRouter(prefix="/ui", tags=["Web UI"])
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

# UI-only display choice -- doesn't affect application.service's own
# _DEFAULT_PAGE_SIZE, which the REST-shaped callers (none yet) would get.
_PAGE_SIZE = 10

_STATE_KEY = "oauth_state"

ROLE_STATUS = {
    ROLE_UNDERWRITER: STATUS_PENDING_UNDERWRITING,
    ROLE_MANAGER: STATUS_PENDING_MANAGER_APPROVAL,
}

# (role, decision) -> the Authorization Services Scope that decision
# requires -- keycloak/import/loanrealm-realm.json's five Permissions.
DECISION_PERMISSION = {
    (ROLE_UNDERWRITER, DECISION_APPROVE): "UnderwriterApprove",
    (ROLE_UNDERWRITER, DECISION_REJECT): "UnderwriterReject",
    (ROLE_UNDERWRITER, DECISION_REQUEST_MORE_INFO): "UnderwriterRequestMoreInfo",
    (ROLE_MANAGER, DECISION_APPROVE): "ManagerApprove",
    (ROLE_MANAGER, DECISION_REJECT): "ManagerReject",
}

# Which decisions each role's screen actually offers (single-item
# buttons and bulk toolbar buttons alike) -- also doubles as this
# route module's own validation of a submitted `decision` value against
# what's legal for the given `role`.
ROLE_DECISIONS = {
    ROLE_UNDERWRITER: (DECISION_APPROVE, DECISION_REJECT, DECISION_REQUEST_MORE_INFO),
    ROLE_MANAGER: (DECISION_APPROVE, DECISION_REJECT),
}

DECISION_LABELS = {
    DECISION_APPROVE: "Approve",
    DECISION_REJECT: "Reject",
    DECISION_REQUEST_MORE_INFO: "Request More Info",
}


def _decision_options(role: str) -> list[tuple[str, str, str]]:
    """`(decision, label, required_permission)` for every decision this
    role's screen offers -- templates use this instead of reconstructing
    Keycloak scope names themselves."""
    return [(decision, DECISION_LABELS[decision], DECISION_PERMISSION[(role, decision)]) for decision in ROLE_DECISIONS[role]]


def _has_select_column(role: str, permissions: set[str]) -> bool:
    """Whether this user holds at least one of this role's decision
    permissions -- computed in Python (not a Jinja `selectattr` over
    tuples, whose numeric-index behavior isn't worth relying on)."""
    return any(permission in permissions for _, _, permission in _decision_options(role))


# Registered as Jinja globals (callable from any template as
# `decision_options(role)`/`has_select_column(role, permissions)`)
# rather than threaded through every render call's context dict --
# every row/list/toolbar/dialog template needs both, computed from
# values (`role`, `permissions`) they already have in context.
templates.env.globals["decision_options"] = _decision_options
templates.env.globals["has_select_column"] = _has_select_column

# Error responses from routes whose success path retargets the swap to a
# single row (`select: 'tr'`, per the htmx4 skill's "Swapping <tr>/<td>
# fragments outside a <table> context") still need to land in the
# dialog instead -- these headers tell htmx to swap the re-rendered
# dialog fragment into #dialog-container regardless of what hx-target
# the triggering element had, and to reselect #dialog-root rather than
# the `tr` selector the triggering call set (which would otherwise
# silently blank the dialog -- there's no `<tr>` in an error response).
_RETARGET_DIALOG_HEADERS = {
    "HX-Retarget": "#dialog-container",
    "HX-Reswap": "innerHTML",
    "HX-Reselect": "#dialog-root",
}

_temporal_client: Optional[Client] = None
_temporal_client_lock = asyncio.Lock()


async def _get_temporal_client() -> Client:
    # Locked, same as application/service.py's own lazy-singleton client --
    # without it, two concurrent first requests can each observe
    # _temporal_client as None and open a second, orphaned connection.
    global _temporal_client
    if _temporal_client is None:
        async with _temporal_client_lock:
            if _temporal_client is None:
                _temporal_client = await Client.connect(
                    os.environ.get("TEMPORAL_HOST", DEFAULT_TEMPORAL_HOST),
                    namespace=os.environ.get("TEMPORAL_NAMESPACE", DEFAULT_TEMPORAL_NAMESPACE),
                )
    return _temporal_client


def _render(request: Request, template: str, ctx: dict, status_code: int = 200, headers: dict | None = None) -> HTMLResponse:
    return templates.TemplateResponse(request, template, ctx, status_code=status_code, headers=headers)


def _redirect_uri(request: Request) -> str:
    return str(request.base_url) + "ui/callback"


def _login_redirect_uri(request: Request) -> str:
    return str(request.base_url) + "ui/login"


def _role_dependency(role: str):
    checker = keycloak_session.require_session_role(role)

    async def dependency(request: Request) -> dict[str, Any]:
        return await checker(request.session.get(SESSION_KEY))

    return dependency


async def _session_user_dependency(request: Request) -> dict[str, Any]:
    return await keycloak_session.get_session_user(request.session.get(SESSION_KEY))


async def _user_permissions(user: dict[str, Any]) -> set[str]:
    """The logged-in user's actual granted permissions, for button-
    visibility purposes -- defense in depth alongside the route-level
    permission checks, not a replacement for them."""
    try:
        return await keycloak_auth.get_permissions(user["access_token"])
    except (keycloak_auth.TokenInvalid, keycloak_auth.PermissionCheckError):
        return set()


async def _resolve_page(role: str, page: int, query_id: str) -> ApplicationPage:
    page = max(page, 1)
    return await application_service.list_by_status(
        ROLE_STATUS[role], page=page, page_size=_PAGE_SIZE, query_id=query_id or None
    )


def _toolbar_oob(role: str, permissions: set[str], selected_ids: set[str], page: int, query_id: str) -> str:
    return templates.get_template("_bulk_toolbar.html").render(
        {
            "role": role,
            "permissions": permissions,
            "selected_ids": selected_ids,
            "page": page,
            "query_id": query_id,
            "oob": True,
        }
    )


def _bulk_result_response(
    request: Request, role: str, action: str, results: list[dict], list_ctx: dict
) -> HTMLResponse:
    dialog_html = templates.get_template("_bulk_result_dialog.html").render(
        {"request": request, "action": action, "results": results}
    )
    list_html = templates.get_template("_staff_list.html").render(
        {"request": request, "role": role, "oob": True, **list_ctx}
    )
    toolbar_html = _toolbar_oob(
        role, list_ctx["permissions"], list_ctx["selected_ids"], list_ctx["paged"].page, list_ctx["paged"].query_id
    )
    return HTMLResponse(dialog_html + list_html + toolbar_html)


async def _application_detail_context(application_id: str, role: str, user: dict[str, Any]) -> dict[str, Any]:
    application = await application_service.get(application_id)

    customer = None
    if application.customer_id is not None:
        try:
            customer = await customer_service.get(application.customer_id)
        except Exception:
            customer = None
    account = await account_service.get_by_application_id(application_id)

    documents = await document_service.list_documents(application_id)
    permissions = await _user_permissions(user)
    return {
        "application": application,
        "customer": customer,
        "account": account,
        "documents": documents,
        "role": role,
        "permissions": permissions,
    }


# ---------------------------------------------------------------- login ----

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    authorize_url, state = keycloak_session.build_authorize_url(_redirect_uri(request))
    request.session[_STATE_KEY] = state
    return _render(request, "login.html", {"authorize_url": authorize_url})


@router.get("/callback")
async def auth_callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None
):
    def _retry(error_message: str, status: int = 400) -> HTMLResponse:
        authorize_url, new_state = keycloak_session.build_authorize_url(_redirect_uri(request))
        request.session[_STATE_KEY] = new_state
        return _render(request, "login.html", {"error": error_message, "authorize_url": authorize_url}, status)

    if error:
        return _retry(f"Keycloak login failed: {error}")
    if not code or not state:
        return _retry("Missing code/state from Keycloak.")

    expected_state = request.session.pop(_STATE_KEY, None)
    try:
        session_id, role = await keycloak_session.complete_login(code, state, expected_state, _redirect_uri(request))
    except ValueError as e:
        return _retry(str(e))

    request.session[SESSION_KEY] = session_id
    return RedirectResponse(url=f"/ui/{role}", status_code=303)


@router.post("/logout")
async def logout_submit(request: Request):
    session_id = request.session.pop(SESSION_KEY, None)
    redirect_url = keycloak_session.logout_redirect_url(_login_redirect_uri(request))
    await keycloak_session.logout(session_id)
    return RedirectResponse(url=redirect_url, status_code=303)


# -------------------------------------------------------- staff list pages ----

async def _staff_page(request: Request, user: dict[str, Any], role: str) -> HTMLResponse:
    session_id = request.session[SESSION_KEY]
    # A fresh navigation/reload clears any stale selection from a
    # previous visit (list-pagination-bulk-actions skill, Part 2).
    await selection_store.clear(session_id)
    paged = await application_service.list_by_status(ROLE_STATUS[role], page_size=_PAGE_SIZE)
    permissions = await _user_permissions(user)
    selected_ids = await selection_store.get(session_id)
    return _render(
        request,
        "staff.html",
        {
            "user": user,
            "role": role,
            "paged": paged,
            "permissions": permissions,
            "selected_ids": selected_ids,
            "page": paged.page,
            "query_id": paged.query_id,
        },
    )


@router.get("/underwriter", response_class=HTMLResponse)
async def underwriter_page(request: Request, user: dict = Depends(_role_dependency(ROLE_UNDERWRITER))):
    return await _staff_page(request, user, ROLE_UNDERWRITER)


@router.get("/manager", response_class=HTMLResponse)
async def manager_page(request: Request, user: dict = Depends(_role_dependency(ROLE_MANAGER))):
    return await _staff_page(request, user, ROLE_MANAGER)


async def _staff_list(request: Request, user: dict[str, Any], role: str, page: int, query_id: str) -> HTMLResponse:
    # Used only by Prev/Next -- the periodic poll targets #app-rows
    # directly (see _staff_rows below), same split as the reference
    # project's operator/manager screens, for the same reason (keeps the
    # header row's select-all checkbox from being rebuilt every poll
    # tick).
    paged = await _resolve_page(role, page, query_id)
    permissions = await _user_permissions(user)
    selected_ids = await selection_store.get(request.session[SESSION_KEY])
    table_html = templates.get_template("_staff_list.html").render(
        {"request": request, "role": role, "paged": paged, "permissions": permissions, "selected_ids": selected_ids}
    )
    toolbar_html = _toolbar_oob(role, permissions, selected_ids, paged.page, paged.query_id)
    return HTMLResponse(table_html + toolbar_html)


@router.post("/underwriter/list", response_class=HTMLResponse)
async def underwriter_list(
    request: Request, page: int = Form(1), query_id: str = Form(""), user: dict = Depends(_role_dependency(ROLE_UNDERWRITER))
):
    return await _staff_list(request, user, ROLE_UNDERWRITER, page, query_id)


@router.post("/manager/list", response_class=HTMLResponse)
async def manager_list(
    request: Request, page: int = Form(1), query_id: str = Form(""), user: dict = Depends(_role_dependency(ROLE_MANAGER))
):
    return await _staff_list(request, user, ROLE_MANAGER, page, query_id)


async def _staff_rows(request: Request, user: dict[str, Any], role: str, page: int, query_id: str) -> HTMLResponse:
    paged = await _resolve_page(role, page, query_id)
    permissions = await _user_permissions(user)
    selected_ids = await selection_store.get(request.session[SESSION_KEY])
    return _render(
        request, "_staff_rows.html", {"role": role, "paged": paged, "permissions": permissions, "selected_ids": selected_ids}
    )


@router.post("/underwriter/rows", response_class=HTMLResponse)
async def underwriter_rows(
    request: Request, page: int = Form(1), query_id: str = Form(""), user: dict = Depends(_role_dependency(ROLE_UNDERWRITER))
):
    return await _staff_rows(request, user, ROLE_UNDERWRITER, page, query_id)


@router.post("/manager/rows", response_class=HTMLResponse)
async def manager_rows(
    request: Request, page: int = Form(1), query_id: str = Form(""), user: dict = Depends(_role_dependency(ROLE_MANAGER))
):
    return await _staff_rows(request, user, ROLE_MANAGER, page, query_id)


async def _staff_bulk_select(
    request: Request, user: dict[str, Any], role: str, application_ids: str, checked: bool, page: int, query_id: str
) -> HTMLResponse:
    # application_ids arrives as a single comma-joined string, not
    # repeated form fields -- htmx's hx-vals hands an array straight to
    # FormData.set(), which stringifies via Array.prototype.toString()
    # (comma-joined), not one field per element (htmx4 skill). ids are
    # app-<digits> ids, never containing a literal comma, so splitting is safe.
    ids = [i for i in application_ids.split(",") if i]
    # Gated by role only, not a permission check -- marking a row
    # "selected" has no side effect beyond what's rendered back to this
    # same user (list-pagination-bulk-actions skill, Part 2). The real
    # enforcement point is the bulk-decision execute route.
    selected_ids = await selection_store.update(request.session[SESSION_KEY], ids, checked)
    paged = await _resolve_page(role, page, query_id)
    permissions = await _user_permissions(user)
    rows_html = templates.get_template("_staff_rows.html").render(
        {"request": request, "role": role, "paged": paged, "permissions": permissions, "selected_ids": selected_ids}
    )
    toolbar_html = _toolbar_oob(role, permissions, selected_ids, paged.page, paged.query_id)
    return HTMLResponse(rows_html + toolbar_html)


@router.post("/underwriter/bulk-select", response_class=HTMLResponse)
async def underwriter_bulk_select(
    request: Request,
    application_ids: str = Form(""),
    checked: bool = Form(...),
    page: int = Form(1),
    query_id: str = Form(""),
    user: dict = Depends(_role_dependency(ROLE_UNDERWRITER)),
):
    return await _staff_bulk_select(request, user, ROLE_UNDERWRITER, application_ids, checked, page, query_id)


@router.post("/manager/bulk-select", response_class=HTMLResponse)
async def manager_bulk_select(
    request: Request,
    application_ids: str = Form(""),
    checked: bool = Form(...),
    page: int = Form(1),
    query_id: str = Form(""),
    user: dict = Depends(_role_dependency(ROLE_MANAGER)),
):
    return await _staff_bulk_select(request, user, ROLE_MANAGER, application_ids, checked, page, query_id)


# --------------------------------------------------------- detail dialog ----

async def _staff_detail(request: Request, application_id: str, role: str, user: dict[str, Any]) -> HTMLResponse:
    try:
        ctx = await _application_detail_context(application_id, role, user)
    except ApplicationNotFound:
        raise HTTPException(status_code=404)
    return _render(request, "_detail_dialog.html", ctx)


@router.get("/underwriter/{application_id}/detail", response_class=HTMLResponse)
async def underwriter_detail(
    request: Request, application_id: str, user: dict = Depends(_role_dependency(ROLE_UNDERWRITER))
):
    return await _staff_detail(request, application_id, ROLE_UNDERWRITER, user)


@router.get("/manager/{application_id}/detail", response_class=HTMLResponse)
async def manager_detail(request: Request, application_id: str, user: dict = Depends(_role_dependency(ROLE_MANAGER))):
    return await _staff_detail(request, application_id, ROLE_MANAGER, user)


async def _document_preview(application_id: str, document_id: int) -> StreamingResponse:
    try:
        stream = await document_service.preview(application_id, document_id)
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


@router.get("/underwriter/{application_id}/documents/{document_id}/preview")
async def underwriter_document_preview(
    application_id: str, document_id: int, user: dict = Depends(_role_dependency(ROLE_UNDERWRITER))
):
    return await _document_preview(application_id, document_id)


@router.get("/manager/{application_id}/documents/{document_id}/preview")
async def manager_document_preview(
    application_id: str, document_id: int, user: dict = Depends(_role_dependency(ROLE_MANAGER))
):
    return await _document_preview(application_id, document_id)


# --------------------------------------------------------- single decision ----

async def _staff_decision(
    request: Request, application_id: str, role: str, decision: str, comment: str, user: dict[str, Any]
) -> HTMLResponse:
    if decision not in ROLE_DECISIONS.get(role, ()):
        raise HTTPException(status_code=400, detail=f"invalid decision {decision!r} for role {role!r}")
    permission = DECISION_PERMISSION[(role, decision)]
    await keycloak_session.check_permission(user, permission)

    try:
        application = await application_service.get(application_id)
    except ApplicationNotFound:
        raise HTTPException(status_code=404)

    if decision == DECISION_APPROVE:
        blocking = await application_service.check_decision_allowed(application_id, DECISION_APPROVE)
        if blocking:
            ctx = await _application_detail_context(application_id, role, user)
            ctx["error"] = "; ".join(blocking)
            return _render(request, "_detail_dialog.html", ctx, 400, headers=_RETARGET_DIALOG_HEADERS)

    client = await _get_temporal_client()
    await workflow_service.signal_decision(client, application.workflow_id, role, decision, user["username"], comment)
    updated = await application_service.wait_for_status_change(application_id, application.status)

    permissions = await _user_permissions(user)
    return _render(request, "_staff_row_response.html", {"application": updated, "role": role, "permissions": permissions})


@router.post("/underwriter/{application_id}/decision", response_class=HTMLResponse)
async def underwriter_decision(
    request: Request,
    application_id: str,
    decision: str = Form(...),
    comment: str = Form(""),
    user: dict = Depends(_session_user_dependency),
):
    return await _staff_decision(request, application_id, ROLE_UNDERWRITER, decision, comment, user)


@router.post("/manager/{application_id}/decision", response_class=HTMLResponse)
async def manager_decision(
    request: Request,
    application_id: str,
    decision: str = Form(...),
    comment: str = Form(""),
    user: dict = Depends(_session_user_dependency),
):
    return await _staff_decision(request, application_id, ROLE_MANAGER, decision, comment, user)


# -------------------------------------------------------------- bulk decision ----

async def _bulk_decision_form(
    request: Request, role: str, decision: str, page: int, query_id: str, user: dict[str, Any]
) -> HTMLResponse:
    if decision not in ROLE_DECISIONS.get(role, ()):
        raise HTTPException(status_code=400, detail=f"invalid decision {decision!r} for role {role!r}")
    await keycloak_session.check_permission(user, DECISION_PERMISSION[(role, decision)])

    ids = await selection_store.get(request.session[SESSION_KEY])
    items = []
    for raw_id in ids:
        try:
            items.append(await application_service.get(raw_id))
        except (ApplicationNotFound, ValueError):
            continue  # stale/foreign id -- dropped from the preview, same as the reference project

    action_label = DECISION_LABELS[decision]
    return _render(
        request,
        "_bulk_confirm_dialog.html",
        {
            "role": role,
            "decision": decision,
            "action": action_label,
            "items": items,
            "page": page,
            "query_id": query_id,
        },
    )


@router.post("/underwriter/bulk-decision-form", response_class=HTMLResponse)
async def underwriter_bulk_decision_form(
    request: Request,
    decision: str = Form(...),
    page: int = Form(1),
    query_id: str = Form(""),
    user: dict = Depends(_role_dependency(ROLE_UNDERWRITER)),
):
    return await _bulk_decision_form(request, ROLE_UNDERWRITER, decision, page, query_id, user)


@router.post("/manager/bulk-decision-form", response_class=HTMLResponse)
async def manager_bulk_decision_form(
    request: Request,
    decision: str = Form(...),
    page: int = Form(1),
    query_id: str = Form(""),
    user: dict = Depends(_role_dependency(ROLE_MANAGER)),
):
    return await _bulk_decision_form(request, ROLE_MANAGER, decision, page, query_id, user)


async def _bulk_decision_execute(
    request: Request, role: str, decision: str, comment: str, page: int, query_id: str, user: dict[str, Any]
) -> HTMLResponse:
    if decision not in ROLE_DECISIONS.get(role, ()):
        raise HTTPException(status_code=400, detail=f"invalid decision {decision!r} for role {role!r}")
    await keycloak_session.check_permission(user, DECISION_PERMISSION[(role, decision)])

    session_id = request.session[SESSION_KEY]
    ids = await selection_store.get(session_id)

    applications = []
    for raw_id in ids:
        try:
            applications.append(await application_service.get(raw_id))
        except (ApplicationNotFound, ValueError):
            continue

    results: list[dict[str, Any]] = []
    workflow_ids: list[str] = []
    eligible: list[Any] = []

    if decision == DECISION_APPROVE:
        # Pre-filter conflicting applications out of the batch *before*
        # collecting workflow_ids (P6-5b/P10-3) -- reported in the same
        # per-item result shape bulk_signal_decision returns for any
        # other failure, rather than passed through to it at all.
        # check_decision_allowed_bulk (not a plain per-item loop over
        # check_decision_allowed) is what actually closes the in-batch
        # active-account race (CLAUDE.md's Known Gaps): two applications
        # for the same customer+product_type selected into this same
        # bulk action would otherwise both pass an independent per-item
        # check, since neither one's account exists yet.
        blocking_by_id = await application_service.check_decision_allowed_bulk(
            [application.application_id for application in applications], DECISION_APPROVE
        )
        for application in applications:
            blocking = blocking_by_id[application.application_id]
            if blocking:
                results.append({"label": application.application_id, "ok": False, "error": "; ".join(blocking)})
            else:
                eligible.append(application)
                workflow_ids.append(application.workflow_id)
    else:
        eligible = applications
        workflow_ids = [a.workflow_id for a in applications]

    if workflow_ids:
        client = await _get_temporal_client()
        try:
            bulk_results = await workflow_service.bulk_signal_decision(
                client, workflow_ids, role, decision, user["username"], comment
            )
        except ValueError as e:
            # Over _MAX_BULK_SIZE, most likely -- re-render the confirm
            # dialog with the error rather than a raw 400, same pattern
            # as every other dialog's error path.
            action_label = DECISION_LABELS[decision]
            return _render(
                request,
                "_bulk_confirm_dialog.html",
                {
                    "role": role,
                    "decision": decision,
                    "action": action_label,
                    "items": applications,
                    "page": page,
                    "query_id": query_id,
                    "error": str(e),
                },
                400,
                headers=_RETARGET_DIALOG_HEADERS,
            )
        by_workflow_id = {a.workflow_id: a for a in eligible}
        for r in bulk_results:
            application = by_workflow_id.get(r.workflow_id)
            label = application.application_id if application else r.workflow_id
            results.append({"label": label, "ok": r.ok, "error": r.error})
            if r.ok and application is not None:
                await application_service.wait_for_status_change(application.application_id, application.status)

    await selection_store.clear(session_id)
    paged = await _resolve_page(role, page, query_id)
    permissions = await _user_permissions(user)
    selected_ids = await selection_store.get(session_id)  # empty -- just cleared
    action = decision.replace("_", " ").title()
    return _bulk_result_response(
        request, role, action, results, {"paged": paged, "permissions": permissions, "selected_ids": selected_ids}
    )


@router.post("/underwriter/bulk-decision", response_class=HTMLResponse)
async def underwriter_bulk_decision(
    request: Request,
    decision: str = Form(...),
    comment: str = Form(""),
    page: int = Form(1),
    query_id: str = Form(""),
    user: dict = Depends(_role_dependency(ROLE_UNDERWRITER)),
):
    return await _bulk_decision_execute(request, ROLE_UNDERWRITER, decision, comment, page, query_id, user)


@router.post("/manager/bulk-decision", response_class=HTMLResponse)
async def manager_bulk_decision(
    request: Request,
    decision: str = Form(...),
    comment: str = Form(""),
    page: int = Form(1),
    query_id: str = Form(""),
    user: dict = Depends(_role_dependency(ROLE_MANAGER)),
):
    return await _bulk_decision_execute(request, ROLE_MANAGER, decision, comment, page, query_id, user)
