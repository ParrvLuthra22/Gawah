# Ownership

| System | Owner | Directories | Port |
|--------|-------|-------------|------|
| System 1: shopper agent, demo store, attack simulation | Suvir | `system1/` | 8001 |
| System 2: authorization, Guardian, payments, ledger, orchestration | Parrv | `engine/` | 8000 |
| System 3: Dispute Counsel and dashboard | Navya | `system3/`, `web/` | 8002 |

Shared: `contract/` (models, schemas, fixtures) and `docs/CONTRACT.md`.

## Rules

- Only edit your own system's directories.
- Anything in `contract/` needs all three of us to agree before it merges.
