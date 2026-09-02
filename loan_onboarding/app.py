"""The FastAPI app itself -- one of the two composition roots (the
other is `worker_main.py`), the only files allowed to import from every
module (CLAUDE.md's "Breaking the application <-> workflow cycle").

Mounts both front doors from this one process: `bff_backoffice/routes.py`'s
`/ui/*` routes (real Keycloak session auth, Authorization Code flow --
see `bff_backoffice/keycloak_session.py`) and, as of Phase 11,
`bff_customer/routes.py`'s `/apply/*` routes (no Keycloak -- see
`bff_customer/identity.py`). The two sides do NOT share one cookie:
`bff_backoffice` uses the `SessionMiddleware` below (an opaque Redis
session id, plus the transient OAuth CSRF state); `bff_customer`'s own
`applicant_identifier` cookie is hand-rolled in `identity.py` with its
own `itsdangerous` serializer and its own secret
(`CUSTOMER_SESSION_SECRET_KEY`), since Starlette allows only one
`SessionMiddleware`/cookie per app and that one is already spoken for.
The customer-side new-application wizard's own ephemeral draft state
(`bff_customer/routes.py`'s `_DRAFT_KEY`) is the one piece of
`bff_customer` state that *does* still ride on this shared
`SessionMiddleware` -- see `identity.py`'s module docstring for why that
split is deliberate, not an oversight.

Run from anywhere on the Python path (project root, or installed as a
package):

    uvicorn loan_onboarding.app:app --reload --port 8000
"""

import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from loan_onboarding.bff_backoffice.keycloak_session import PermissionDenied, RequireLoginRedirect, RoleDenied
from loan_onboarding.bff_backoffice.routes import router as backoffice_router
from loan_onboarding.bff_customer.routes import IdentifyRequired
from loan_onboarding.bff_customer.routes import router as customer_router

app = FastAPI(title="Loan Onboarding")

# One session cookie for the whole app -- the /ui/* HTMX UI's opaque
# Redis session id (bff_backoffice/keycloak_session.py) and the /apply/*
# flow's plain applicant_identifier (bff_customer/identity.py) live
# under different keys in the same signed cookie. Neither holds anything
# token-shaped (the real Keycloak token set lives server-side, in
# session_store.py's Redis), so one lightweight signed cookie covers
# both sides.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("BACKOFFICE_SESSION_SECRET_KEY", "dev-only-insecure-secret"),
)


@app.exception_handler(RequireLoginRedirect)
async def _redirect_to_login(request, exc):
    return RedirectResponse(url="/ui/login", status_code=303)


@app.exception_handler(RoleDenied)
async def _role_denied(request, exc):
    return HTMLResponse(str(exc), status_code=403)


@app.exception_handler(PermissionDenied)
async def _permission_denied(request, exc):
    return HTMLResponse(str(exc), status_code=403)


@app.exception_handler(IdentifyRequired)
async def _redirect_to_identify(request, exc):
    return RedirectResponse(url="/apply/identify", status_code=303)


app.include_router(backoffice_router)
app.include_router(customer_router)


@app.get("/")
async def root():
    return RedirectResponse(url="/ui/login")
