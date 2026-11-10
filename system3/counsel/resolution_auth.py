import hashlib
import hmac
import re
from typing import Annotated, Literal

from fastapi import Header, Request
from pydantic import BaseModel

from .clients import IntegrationError


class Actor(BaseModel):
    id: str
    role: Literal["reviewer", "operator", "viewer"]

    def require_review(self) -> None:
        if self.role == "viewer":
            raise IntegrationError("REVIEW_FORBIDDEN", "A reviewer role is required.", 403)

    def require_operator(self) -> None:
        if self.role != "operator":
            raise IntegrationError("EXECUTION_FORBIDDEN", "An operator role is required.", 403)


def authenticate(request: Request, authorization: Annotated[str | None, Header()] = None) -> Actor:
    match = re.fullmatch(
        r"Bearer ([A-Za-z0-9_-]{1,64})\.([A-Za-z0-9_-]{32,128})", authorization or "", re.IGNORECASE
    )
    if match:
        actor_id, secret = match.groups()
        entry = request.app.state.settings.n5_operators.get(actor_id)
        if entry and hmac.compare_digest(
            hashlib.sha256(secret.encode()).hexdigest(), entry.token_sha256.get_secret_value()
        ):
            return Actor(id=actor_id, role=entry.role)
    raise IntegrationError(
        "RESOLUTION_AUTH_REQUIRED", "A valid Resolution Center credential is required.", 401
    )
