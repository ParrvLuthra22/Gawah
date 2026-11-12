import type { NextRequest } from "next/server";
import {
  ShopError, beginSubmit, engineCall, engineHealth, engineProblem, getPrepared, guardedJson, shopFail, shopJson,
} from "@/lib/shop-server";

type Payment = {
  id: string; status: string; amount_minor: number; currency: string; paypal_order_id: string | null;
  paypal_capture_id: string | null; failure_reason: string | null; created_at: string;
};
type Result = {
  decision: { id: string; verdict: string; reasons: string[]; explanation: string };
  payment: Payment | null;
};

function outcome(result: Result, simulated: boolean) {
  const p = result.payment;
  return {
    outcome: p ? p.status : "BLOCKED",
    simulated,
    decision: { id: result.decision.id, verdict: result.decision.verdict, reasons: result.decision.reasons, explanation: result.decision.explanation },
    payment: p && {
      id: p.id, status: p.status, amount_minor: p.amount_minor, currency: p.currency,
      paypal_order_id: p.paypal_order_id, paypal_capture_id: p.paypal_capture_id,
      failure_reason: p.failure_reason, created_at: p.created_at,
    },
  };
}

// Steps 8 and 9. The browser sends only an opaque id, the amount it was shown and an explicit
// confirmation. The proposal that is submitted is the one the server prepared and the Guardian
// dry run saw; the engine's Guardian decides again and the payment happens (or not) inside it.
// A submission is claimed once. Nothing here retries, and an unknown outcome is reported, not repeated.
export async function POST(request: NextRequest) {
  try {
    const body = await guardedJson(request);
    const prepared = getPrepared(body.prepared_id);
    if (body.confirmed !== true) throw new ShopError(422, "Confirm the purchase to continue.", "NOT_CONFIRMED");
    if (body.expected_total_minor !== prepared.totalMinor || body.expected_currency !== prepared.currency) {
      throw new ShopError(409, "The amount you confirmed does not match the prepared proposal. Prepare it again.", "AMOUNT_MISMATCH");
    }
    if (prepared.state === "done" && prepared.result) return shopJson({ ...prepared.result, duplicate: true });
    if (prepared.dryRunVerdict !== "ALLOW") {
      throw new ShopError(409, "The Guardian dry run did not allow this proposal, so it cannot be submitted.", "GUARDIAN_DRY_RUN_BLOCK");
    }
    const health = await engineHealth();
    if (health.status !== "ok" || health.paypal !== "sandbox") {
      throw new ShopError(409, "The engine is not reporting a PayPal Sandbox. Nothing was submitted.", "NOT_SANDBOX");
    }
    const simulated = health.mock === true || health.env === "test";
    if (!simulated && health.card_configured !== true) {
      throw new ShopError(409, "The engine has no Sandbox card configured (PAYPAL_CARD_*). Nothing was submitted.", "CARD_NOT_CONFIGURED");
    }

    beginSubmit(prepared); // synchronous: from here on, this purchase is claimed
    let upstream;
    try {
      upstream = await engineCall("system1", "POST", "/v1/proposals", prepared.proposal, 150_000);
    } catch (error) {
      prepared.state = "unknown"; // sent, no answer: the payment may or may not exist
      if (error instanceof ShopError) {
        return shopJson({
          outcome: "UNKNOWN", simulated,
          message: "The engine did not answer, so the payment outcome is unknown. It was not retried. "
            + "Check the Ledger and Payments before doing anything else.",
        }, 202);
      }
      throw error;
    }
    if (upstream.status !== 200) {
      if (upstream.status >= 500) {
        prepared.state = "unknown";
        return shopJson({
          outcome: "UNKNOWN", simulated,
          message: `The engine answered HTTP ${upstream.status}, so the payment outcome is unknown. It was not retried. Check the Ledger and Payments.`,
        }, 202);
      }
      prepared.state = "prepared"; // a definite refusal before any payment: the user may try again by hand
      throw engineProblem(upstream.status, upstream.data);
    }
    const result = outcome(upstream.data as Result, simulated);
    prepared.state = "done";
    prepared.result = result;
    return shopJson(result);
  } catch (error) {
    return shopFail(error);
  }
}
