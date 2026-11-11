"""The engine (unmodified) with a SIMULATED semantic model, for `make e2e`.

    uvicorn integration.engine_stub_app:app

The Guardian's category check fails closed without a model, and `make e2e` has no Groq key, so the
semantic check is answered by `integration.stubs.StubSemanticModel`. /health then reports
`semantic: groq, model: simulated-groq-stub`: the model name says it is the stub.
"""

from integration.stubs import install_engine_semantic_stub

install_engine_semantic_stub()

from gawah_engine.main import app

__all__ = ["app"]
