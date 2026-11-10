"""The shopper (System 1's own FastAPI app, unmodified) with a SIMULATED Groq, for `make e2e`.

    uvicorn integration.shopper_stub_app:app --port 8001

Settings come from the environment exactly as for the real shopper (MERCHANT_SIGNING_KEY_PATH
etc.). `GROQ_API_KEY` must be set only because System 1's Settings requires a value; with the
stub it is never sent anywhere. For the real model run `shopper.main:app` instead (E2E_LIVE_GROQ=1).
"""

import os

os.environ.setdefault("GROQ_API_KEY", "stub-not-used")

from shopper.config import Settings
from shopper.main import app

from integration.stubs import install_stubs

install_stubs(Settings())
__all__ = ["app"]
