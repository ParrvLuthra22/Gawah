from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .auth import require_mock, require_service_key
from .errors import install_error_handlers
from .routers import ledger, mandates, merchants, payments, proposals

app = FastAPI(title="Gawah engine (System 2)")

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

for r in (
    mandates.router,
    proposals.router,
    payments.router,
    ledger.router,
    merchants.router,
):
    app.include_router(r, dependencies=[Depends(require_service_key), Depends(require_mock)])


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "gawah"}
