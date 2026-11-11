import type { NextRequest } from "next/server";
import { ShopError, engineCall, engineProblem, getPrepared, guardAndId, shopFail, shopJson } from "@/lib/shop-server";

type Payment = { id: string; proposal_id: string; status: string; amount_minor: number; currency: string;
  paypal_order_id: string | null; paypal_capture_id: string | null; failure_reason: string | null };

// "What happened to my purchase?" for an unknown outcome: read the engine's payments for the mandate
// and match the proposal. Read only; it never submits anything.
export async function GET(request: NextRequest) {
  try {
    const prepared = getPrepared(guardAndId(request, "prepared_id"));
    const out = await engineCall("system3", "GET", `/v1/payments?mandate_id=${encodeURIComponent(prepared.mandateId)}`);
    if (out.status !== 200) throw engineProblem(out.status, out.data);
    const payment = (out.data as Payment[]).find(p => p.proposal_id === prepared.proposalId) ?? null;
    if (!payment && prepared.state === "unknown") {
      return shopJson({ found: false, message: "The engine has no payment for this proposal. It may never have arrived." });
    }
    if (!payment) throw new ShopError(404, "No payment exists for this proposal yet.", "NO_PAYMENT");
    return shopJson({ found: true, payment: {
      id: payment.id, status: payment.status, amount_minor: payment.amount_minor, currency: payment.currency,
      paypal_order_id: payment.paypal_order_id, paypal_capture_id: payment.paypal_capture_id,
      failure_reason: payment.failure_reason,
    } });
  } catch (error) {
    return shopFail(error);
  }
}
