"""The FastAPI app itself -- one of the two composition roots (the
other is `worker_main.py`), the only files allowed to import from every
module (CLAUDE.md's "Breaking the application <-> workflow cycle").

Mounts `bff_backoffice/routes.py`'s `/ui/*` routes (real Keycloak
session auth, Authorization Code flow -- see
`bff_backoffice/keycloak_session.py`). `bff_customer/`'s own router
isn't mounted yet -- that's Phase 11; this file will `include_router()`
it alongside `bff_backoffice`'s the same way this reference project's
own `app.py` mounts two front doors from one process.

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

app = FastAPI(title="Loan Onboarding Back Office")

# Session cookie for the /ui/* HTMX UI (real Keycloak login -- see
# bff_backoffice/keycloak_session.py). Holds only an opaque session id
# (and, transiently, the OAuth CSRF state) -- never the token set itself,
# which lives server-side in session_store.py (Redis).
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


app.include_router(backoffice_router)


@app.get("/")
async def root():
    return RedirectResponse(url="/ui/login")
