# integration/

End-to-end proof of the chain: shopper (System 1) -> signed proposal -> engine Guardian (System 2)
-> payment -> signed, hash-chained ledger. Owned by System 2; it imports `system1/` and edits nothing
there (what System 1 should change is in `docs/INTEGRATION_ISSUES.md`).

```sh
make e2e                       # engine + shopper started and stopped for you; table + exit code
make e2e E2E_MODE=mock         # the mock engine: wire formats only, proves nothing
make e2e E2E_LIVE_GROQ=1       # the shopper uses the real Groq (network, needs GROQ_API_KEY)
ENGINE_PORT=18000 SHOPPER_PORT=18001 make e2e   # if 8000 / 8001 are busy
uv run pytest integration      # the same flow in process (no sockets, no keys)
```

What is real and what is simulated, in every run: the Guardian, ledger, signing and the shopper's
parsing, discovery and cart signing are real. PayPal is the engine's SIMULATED fake (`GAWAH_ENV=test`),
the language model is a SIMULATED stub (`stubs.py`), and the attacker proposals are SIMULATED (built in
`e2e.py` from System 1's attack fixtures, because the shopper's attack runner makes none).

Files: `e2e.py` (the script and its async core), `scenarios.py` (data read from System 1's catalogs and
fixtures, also used by `scripts/warm_semantic_cache.py`), `stubs.py` and `shopper_stub_app.py` (the
stubbed Groq), `tests/` (in-process tests).
