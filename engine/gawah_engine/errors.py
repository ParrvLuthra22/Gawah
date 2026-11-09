from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class GawahError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        self.status, self.code, self.message, self.details = status, code, message, details


def _body(request: Request, code: str, message: str, details: Any = None) -> dict:
    rid = getattr(request.state, "request_id", "unknown")
    return {"error": {"code": code, "message": message, "details": details, "request_id": rid}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(GawahError)
    async def _gawah(request: Request, exc: GawahError):
        return JSONResponse(
            _body(request, exc.code, exc.message, exc.details), status_code=exc.status
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        details = [
            {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()
        ]
        return JSONResponse(
            _body(request, "VALIDATION_ERROR", "Request body failed validation", details),
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return JSONResponse(
            _body(request, code, str(exc.detail)), status_code=exc.status_code
        )
