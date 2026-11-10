"""Create System 3's simulated workflow cases after the System 2 payment demo has run.

This is deliberately a System 3 script: ownership rules prohibit changing System 2's
scripts/demo_run.py. It calls the authenticated System 3 endpoint and never calls PayPal.
"""

import sys

import httpx

from counsel.config import Settings


def main() -> int:
    settings = Settings()
    key = settings.system3_key.get_secret_value()
    if not key:
        print("SYSTEM3_KEY is missing in system3/.env", file=sys.stderr)
        return 2
    try:
        response = httpx.post(
            "http://127.0.0.1:8002/api/dashboard/demo-scenarios",
            headers={"X-Gawah-Key": key}, timeout=60,
        )
    except httpx.RequestError:
        print("System 3 Counsel is unavailable at http://127.0.0.1:8002", file=sys.stderr)
        return 2
    if not response.is_success:
        print(f"Demo scenarios failed: HTTP {response.status_code}: {response.text[:300]}", file=sys.stderr)
        return 1
    rows = response.json()
    for row in rows:
        detail = f" — {row['detail']}" if row.get("detail") else ""
        print(
            f"{row['scenario']}: {row['status']} payment={row['payment_id']} "
            f"dispute={row['dispute_id']} analysis={row.get('analysis_status')} "
            f"recommendation={row.get('recommendation')} workflow={row.get('workflow_id')}{detail}"
        )
    return 0 if all(row["status"] == "COMPLETED" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
