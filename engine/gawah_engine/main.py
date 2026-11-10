from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .auth import require_service_key
from .bootstrap import bootstrap_if_empty
from .config import cors_origins, settings
from .errors import install_error_handlers
from .harden import BodyLimitMiddleware
from .routers import admin, ledger, mandates, merchants, payments, proposals
from .services import check_card_config, check_fake_paypal_allowed, get_services


@asynccontextmanager
async def lifespan(_: FastAPI):
    check_fake_paypal_allowed()
    check_card_config()
    if not settings.gawah_mock:
        if settings.gawah_bootstrap:
            bootstrap_if_empty()
        # create the database, run migrations, create signing keys, then settle any payment
        # a crash left in RESERVED (reads PayPal; never guesses)
        get_services().payments.reconcile()
    yield


app = FastAPI(title="Gawah engine (System 2)", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(settings.engine_cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)


app.add_middleware(BodyLimitMiddleware)  # outermost: 64 KB cap and no floats in money fields


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request.state.request_id = request.headers.get("x-request-id") or f"req_{uuid4().hex[:12]}"
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


install_error_handlers(app)

# Every /v1 route needs a valid key AND the route in that key's scope (see auth.SCOPES).
for r in (
    mandates.router, ledger.router, merchants.router, proposals.router, payments.router
):
    app.include_router(r, dependencies=[Depends(require_service_key)])


# The admin route has its own X-Admin-Key check (and only exists in effect when GAWAH_ENV=demo),
# so it is not under the per-service scopes.
app.include_router(admin.router)


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness, the environment, and the semantic check's LAST REAL outcome (a silent fallback
    is visible). `mock` is true when /v1 serves fixtures; `paypal` is always the sandbox."""
    base: dict[str, Any] = {
        "status": "ok", "service": "gawah",
        "env": settings.gawah_env or "dev", "mock": settings.gawah_mock, "paypal": "sandbox",
        "card_configured": not settings.card_problems(),  # a yes/no only, never a value
    }
    if settings.gawah_mock:
        return {**base, "semantic": "fallback", "model": None}  # mock mode never calls a model
    st = get_services().guardian.semantic.status()
    return {**base, "semantic": st["semantic"], "model": st["model"]}
