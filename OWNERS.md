# Ownership

| System | Owner | Directories | Port |
|--------|-------|-------------|------|
| System 1: shopper agent, demo store, attack simulation | Suvir | `system1/` | 8001 |
| System 2: authorization, Guardian, payments, ledger, orchestration | Parrv | `engine/` | 8000 |
| System 3: Dispute Counsel and dashboard | Navya | `system3/`, `web/` | 8002 |

Shared: `contract/` (models, schemas, fixtures) and `docs/CONTRACT.md`.

## Rules

- Only edit your own system's directories.
- Additive, optional-field changes to `contract/` (new models, new optional fields, new enum values) are
  allowed: list them in `docs/CONTRACT.md` and announce them to the other two owners. Breaking changes need
  all three of us to agree before they merge.
