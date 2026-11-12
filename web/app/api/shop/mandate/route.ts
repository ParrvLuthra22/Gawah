import type { NextRequest } from "next/server";
import {
  ShopError, decimalToMinor, engineCall, engineProblem, getRecommendation, guardedJson, mandateDescription,
  shopFail, shopJson,
} from "@/lib/shop-server";

const MAX_BUDGET_MINOR = 100_000; // $1,000: a Sandbox demo, not a spending tool

// Step 4 of the journey: the user approves the spending restrictions, and the engine (System 2)
// creates and signs the mandate. Nothing is signed here; this route only relays what the user
// approved, built from the server's own copy of the product they were shown.
export async function POST(request: NextRequest) {
  try {
    const body = await guardedJson(request);
    if (body.approved !== true) throw new ShopError(422, "Approve the spending restrictions to continue.", "MANDATE_NOT_APPROVED");
    const rec = getRecommendation(body.recommendation_id);
    const entry = rec.products.get(typeof body.product_id === "string" ? body.product_id : "");
    if (!entry) throw new ShopError(404, "That product is not part of this recommendation.", "PRODUCT_UNKNOWN");
    if (!entry.checkout.eligible || entry.checkout.unit_price_minor === null) {
      throw new ShopError(422, `This product cannot be purchased here: ${entry.checkout.reason}`, "NOT_PURCHASABLE");
    }
    const budget = decimalToMinor(body.budget);
    if (budget === null || budget > MAX_BUDGET_MINOR) {
      throw new ShopError(422, "Enter a budget in dollars, for example 60 or 59.99 (at most 1000).", "BUDGET_INVALID");
    }
    if (budget < entry.checkout.unit_price_minor) {
      throw new ShopError(422, "The budget is below the product's price, so the Guardian would block it.", "BUDGET_TOO_LOW");
    }
    const minutes = Number(body.expiry_minutes);
    if (!Number.isInteger(minutes) || minutes < 5 || minutes > 120) {
      throw new ShopError(422, "Choose an expiry between 5 and 120 minutes.", "EXPIRY_INVALID");
    }
    const product = entry.view;
    const created = await engineCall("system3", "POST", "/v1/mandates", {
      user_id: "dashboard-user",
      category: product.category,
      item_description: mandateDescription(rec.intent, product, budget),
      max_amount_minor: budget,
      currency: product.currency,
      allowed_merchants: [product.merchant_id],
      expires_at: new Date(Date.now() + minutes * 60_000).toISOString(),
    });
    if (created.status !== 200 && created.status !== 201) throw engineProblem(created.status, created.data);
    const mandate = created.data as {
      id: string; user_id: string; category: string; item_description: string; max_amount_minor: number;
      currency: string; allowed_merchants: string[]; issued_at: string; expires_at: string; status: string;
      signing_alg?: string; key_id: string;
    };
    rec.mandateId = mandate.id;
    rec.mandateBudgetMinor = mandate.max_amount_minor;
    rec.mandateCurrency = mandate.currency;
    rec.mandateMerchant = product.merchant_id;
    rec.mandateProductId = product.id;
    return shopJson({
      mandate: {
        id: mandate.id, category: mandate.category, item_description: mandate.item_description,
        max_amount_minor: mandate.max_amount_minor, currency: mandate.currency,
        allowed_merchants: mandate.allowed_merchants, issued_at: mandate.issued_at,
        expires_at: mandate.expires_at, status: mandate.status, signing_alg: mandate.signing_alg ?? "ES256",
        key_id: mandate.key_id,
      },
    });
  } catch (error) {
    return shopFail(error);
  }
}
