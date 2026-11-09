"""Spike A1/A3/refund: pay a sandbox order with no human click, read custom_id back, refund.

Run: uv run python spikes/a1_pay_order.py [card|browser]
Never prints secrets. Output also saved to spikes/out/a1_result.json (gitignored).
"""

import json
import sys
import uuid
from pathlib import Path

from gawah_engine.config import settings
from gawah_engine.paypal import PayPalClient, PayPalError, capture_id_of

OUT = Path(__file__).resolve().parent / "out"
CARD = {"card": {"number": "4005519200000004", "expiry": "2030-12", "security_code": "123",
                 "name": "Gawah Spike"}}


def find_custom_id(obj, path="$"):
    """Return every JSON path where a custom_id key appears, with its value."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "custom_id":
                hits.append((f"{path}.custom_id", v))
            hits += find_custom_id(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += find_custom_id(v, f"{path}[{i}]")
    return hits


def main(route: str) -> int:
    if not settings.paypal_client_id or not settings.paypal_client_secret:
        print("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are empty. Stop.")
        return 2
    pp = PayPalClient(settings.paypal_client_id, settings.paypal_client_secret, settings.paypal_env)
    pp.get_token()
    print("auth: ok")
    OUT.mkdir(exist_ok=True)
    result: dict = {"route": route}
    custom_id = "gawah-spike-001"
    key = f"spike-a1-{uuid.uuid4()}"
    try:
        if route == "card":
            order = pp.create_order(1000, "USD", custom_id, "Gawah spike A1", key, CARD)
        else:
            raise SystemExit("browser route lives in spikes/a1_browser.py")
    except PayPalError as e:
        print(f"create_order FAILED: status={e.status} debug_id={e.debug_id}")
        print(json.dumps(e.body, indent=2)[:1500])
        result["error"] = {"status": e.status, "debug_id": e.debug_id, "body": e.body}
        (OUT / "a1_result.json").write_text(json.dumps(result, indent=2))
        return 1
    print(f"order id={order['id']} status={order['status']}")
    if order["status"] != "COMPLETED":
        order = pp.capture_order(order["id"], f"{key}-cap")
        print(f"captured: status={order['status']}")
    cap_id = capture_id_of(order)
    print(f"capture id={cap_id}")
    result.update(order_id=order["id"], status=order["status"], capture_id=cap_id)

    # A3: where does custom_id come back?
    fresh = pp.get_order(order["id"])
    print("A3 get_order custom_id locations:", find_custom_id(fresh))
    print("A3 pay/capture response custom_id locations:", find_custom_id(order))
    result["a3_get_order"] = find_custom_id(fresh)
    result["a3_capture_response"] = find_custom_id(order)

    # Refund (the System 3 'concede' path)
    refund = pp.refund_capture(cap_id, 1000, "USD", "Gawah spike refund", f"{key}-refund")
    print(f"refund id={refund.get('id')} status={refund.get('status')}")
    result["refund"] = {"id": refund.get("id"), "status": refund.get("status")}
    (OUT / "a1_result.json").write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "card"))
