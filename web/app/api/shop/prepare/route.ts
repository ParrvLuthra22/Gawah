import type { NextRequest } from "next/server";
import {
  type Verdict, ShopError, engineCall, engineProblem, getRecommendation, guardedJson, newId, savePrepared,
  shopFail, shopJson, shopperCall, shopperProblem,
} from "@/lib/shop-server";

type Decision = { id: string; verdict: Verdict; reasons: string[]; explanation: string; mandate_id?: string | null };
type Proposal = {
  id: string; mandate_id: string; merchant_id: string; quantity: number; unit_price_minor: number; currency: string;
  product: { id: string; title: string };
  signed_cart: {
    cart_id: string; merchant_id: string; total_minor: number; currency: string; expires_at: string; key_id: string;
    line_items: { sku: string; title: string; variant: string | null; quantity: number; unit_price_minor: number }[];
  };
};

// Steps 6 and 7: System 1 obtains the merchant-signed cart and builds the PurchaseProposal, then the
// Guardian is asked for a DRY RUN (POST /v1/guardian/check: no payment, no ledger write). The
// proposal stays on the server; the browser only ever holds an opaque id for it.
export async function POST(request: NextRequest) {
  try {
    const body = await guardedJson(request);
    const rec = getRecommendation(body.recommendation_id);
    if (!rec.mandateId || !rec.mandateProductId || !rec.mandateMerchant) {
      throw new ShopError(409, "Create the mandate before preparing a proposal.", "MANDATE_REQUIRED");
    }
    const productId = rec.mandateProductId;

    const keyed = await engineCall("system1", "GET", `/v1/merchants/${encodeURIComponent(rec.mandateMerchant)}/public-key`);
    const merchantRegistered = keyed.status === 200;

    const prepared = await shopperCall("POST", "/shopping/proposals/prepare", {
      selected_product_id: productId, merchant_id: rec.mandateMerchant, mandate_id: rec.mandateId,
      agent_id: "dashboard-shopper", quantity: 1,
    });
    if (prepared.status !== 200) throw shopperProblem(prepared.status);
    const proposal = prepared.data as Proposal;

    const check = await engineCall("system3", "POST", "/v1/guardian/check", proposal);
    if (check.status !== 200) throw engineProblem(check.status, check.data);
    const decision = check.data as Decision;

    const id = newId();
    savePrepared({
      id, createdAt: Date.now(), recommendationId: rec.id, mandateId: rec.mandateId,
      proposal: proposal as unknown as Record<string, unknown>, proposalId: proposal.id,
      totalMinor: proposal.signed_cart.total_minor, currency: proposal.signed_cart.currency,
      merchantId: proposal.merchant_id, title: proposal.product.title, dryRunVerdict: decision.verdict, state: "prepared",
    });
    const cart = proposal.signed_cart;
    return shopJson({
      prepared_id: id,
      merchant_key_registered: merchantRegistered,
      proposal: {
        id: proposal.id, mandate_id: proposal.mandate_id, merchant_id: proposal.merchant_id,
        quantity: proposal.quantity, total_minor: cart.total_minor, currency: cart.currency,
        cart: {
          cart_id: cart.cart_id, expires_at: cart.expires_at, key_id: cart.key_id, signed: true,
          line_items: cart.line_items.map(l => ({
            sku: l.sku, title: l.title, variant: l.variant, quantity: l.quantity, unit_price_minor: l.unit_price_minor,
          })),
        },
      },
      guardian: {
        dry_run: true, id: decision.id, verdict: decision.verdict, reasons: decision.reasons,
        explanation: decision.explanation,
      },
    });
  } catch (error) {
    return shopFail(error);
  }
}
