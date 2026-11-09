"""Spike A1 route 2: pay via headless buyer approval. Needs BUYER_EMAIL/BUYER_PASSWORD in .env
(sandbox PERSONAL account, not the business one). Run: uv run python spikes/a1_browser.py
Also runs A3 (custom_id round trip) and the refund check. Screenshots go to spikes/out/."""

import json
import sys
import uuid
from pathlib import Path

from a1_pay_order import find_custom_id
from gawah_engine.config import settings
from gawah_engine.paypal import PayPalClient, capture_id_of, pay_order
from gawah_engine.paypal_browser import approve_in_browser

OUT = Path(__file__).resolve().parent / "out"


def main() -> int:
    if not (settings.buyer_email and settings.buyer_password):
        print("BUYER_EMAIL / BUYER_PASSWORD are empty in .env (need a sandbox PERSONAL account).")
        return 2
    pp = PayPalClient(settings.paypal_client_id, settings.paypal_client_secret, settings.paypal_env)
    key = f"spike-a1b-{uuid.uuid4()}"
    order = pay_order(
        pp, 1000, "USD", "gawah-spike-001", "Gawah spike A1 (browser)", key,
        buyer_email=settings.buyer_email, buyer_password=settings.buyer_password,
        approver=lambda u, e, p: approve_in_browser(u, e, p, OUT),
    )
    cap = capture_id_of(order)
    print(f"order id={order['id']} status={order['status']} capture id={cap}")
    fresh = pp.get_order(order["id"])
    print("A3 get_order custom_id:", find_custom_id(fresh))
    print("A3 capture response custom_id:", find_custom_id(order))
    refund = pp.refund_capture(cap, 1000, "USD", "Gawah spike refund", f"{key}-refund")
    print(f"refund id={refund.get('id')} status={refund.get('status')}")
    (OUT / "a1_browser_result.json").write_text(json.dumps(
        {"order_id": order["id"], "capture_id": cap, "a3": find_custom_id(fresh),
         "refund": refund.get("status")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    sys.exit(main())
