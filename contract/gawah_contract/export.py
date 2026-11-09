"""Export JSON Schema for every model: `python -m gawah_contract.export` (or `make contract`)."""

import json
import sys
from pathlib import Path

from . import models

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"

EXPORTED = [
    "Mandate",
    "MandateCreate",
    "BudgetStatus",
    "PurchaseProposal",
    "SignedCart",
    "GuardianDecision",
    "PaymentRecord",
    "LedgerEntry",
    "DisputeCase",
    "ErrorResponse",
    "ProposalResult",
    "VerificationCheck",
    "LedgerHead",
    "LedgerVerifyResult",
    "EvidenceBundle",
    "MerchantKey",
    "RefundRequest",
]


def render() -> dict[str, str]:
    out = {}
    for name in EXPORTED:
        schema = getattr(models, name).model_json_schema()
        out[f"{name}.json"] = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    return out


def main() -> int:
    SCHEMAS_DIR.mkdir(exist_ok=True)
    for fname, text in render().items():
        (SCHEMAS_DIR / fname).write_text(text)
    print(f"wrote {len(EXPORTED)} schemas to {SCHEMAS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
