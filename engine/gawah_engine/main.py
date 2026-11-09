from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .auth import require_mock, require_service_key
from .config import settings
from .errors import install_error_handlers
from .routers import ledger, mandates, merchants, payments, proposals
from .services import get_services


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.gawah_mock:
        get_services()  # create the database, run migrations, create signing keys
    yield


app = FastAPI(title="Gawah engine (System 2)", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request.state.request_id = request.headers.get("x-request-id") or f"req_{uuid4().hex[:12]}"
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


install_error_handlers(app)

# Mandates and the ledger have real logic (GAWAH_MOCK=0); the rest is still mock-only and
# answers 501 in real mode.
for r in (mandates.router, ledger.router, merchants.router):
    app.include_router(r, dependencies=[Depends(require_service_key)])
for r in (proposals.router, payments.router):
    app.include_router(r, dependencies=[Depends(require_service_key), Depends(require_mock)])


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "gawah"}
