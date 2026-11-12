import type { NextRequest } from "next/server";
import {
  type Checkout, type ProductView, ShopError, cleanText, decimalToMinor, guardedJson, newId, saveRecommendation,
  shopFail, shopJson, shopperCall, shopperProblem,
} from "@/lib/shop-server";

type RawProduct = {
  id: string; title: string; brand: string; category: string; merchant_id: string;
  price: string; currency: string; attributes?: Record<string, string>;
};
type RawRecommendation = {
  product: RawProduct; verification_status: string; missing_verifications?: string[]; rank_reason: string;
};
type MerchantProduct = { product_id: string; merchant_id: string; unit_price_minor: number; currency: string; stock: number };

// Only fields the page shows. Descriptions and URLs come from third-party listings and stay server-side.
function view(p: RawProduct): ProductView {
  return {
    id: cleanText(p.id, 100), title: cleanText(p.title, 200), brand: cleanText(p.brand, 100),
    category: cleanText(p.category, 100), merchant_id: cleanText(p.merchant_id, 100),
    price: cleanText(p.price, 20), currency: cleanText(p.currency, 3).toUpperCase(),
    attributes: Object.fromEntries(Object.entries(p.attributes ?? {}).slice(0, 8)
      .map(([k, v]) => [cleanText(k, 30), cleanText(v, 60)])),
  };
}

// Checkout is only for the controlled demo merchant, whose carts the shopper can sign. A listing
// from any other provider (Channel3 included) is shown, never offered for purchase.
function checkoutFor(p: ProductView, status: string, catalog: Map<string, MerchantProduct>): Checkout {
  if (status !== "eligible") return { eligible: false, reason: "Not fully verified against your request.", unit_price_minor: null, stock: null };
  const merchant = catalog.get(p.id);
  if (!merchant) return { eligible: false, reason: "Not sold by the controlled demo merchant; no trusted merchant cart or payment integration.", unit_price_minor: null, stock: null };
  if (merchant.merchant_id !== p.merchant_id || merchant.currency !== p.currency) {
    return { eligible: false, reason: "The merchant catalog disagrees with the listing.", unit_price_minor: merchant.unit_price_minor, stock: merchant.stock };
  }
  if (merchant.stock < 1) return { eligible: false, reason: "Out of stock at the merchant.", unit_price_minor: merchant.unit_price_minor, stock: merchant.stock };
  const listed = decimalToMinor(p.price);
  if (listed !== merchant.unit_price_minor) {
    return { eligible: false, reason: "The listed price differs from the merchant's price.", unit_price_minor: merchant.unit_price_minor, stock: merchant.stock };
  }
  return { eligible: true, reason: "Controlled demo merchant: signed cart available.", unit_price_minor: merchant.unit_price_minor, stock: merchant.stock };
}

export async function POST(request: NextRequest) {
  try {
    const body = await guardedJson(request);
    const message = cleanText(body.message, 500);
    if (message.length < 3) throw new ShopError(422, "Describe what you want to buy.", "SHOP_MESSAGE");

    const [agent, catalogResponse] = await Promise.all([
      shopperCall("POST", "/shopping/agent", { message }),
      shopperCall("GET", "/shopping/demo/products"),
    ]);
    if (agent.status !== 200) throw shopperProblem(agent.status);
    const catalog = new Map<string, MerchantProduct>(
      catalogResponse.status === 200 && Array.isArray(catalogResponse.data)
        ? (catalogResponse.data as MerchantProduct[]).map(p => [p.product_id, p]) : []);

    const result = agent.data as {
      intent: Record<string, unknown>; selected_product: RawProduct | null; explanation: string;
      alternatives: RawProduct[]; needs_verification: { product: RawProduct; reasons: string[] }[];
      recommended_product: RawRecommendation | null; ranked_recommendations: RawRecommendation[];
      trace: { tool: string; status: string; result_count: number }[];
    };
    const record = { id: newId(), createdAt: Date.now(), intent: result.intent, products: new Map<string, { view: ProductView; checkout: Checkout }>() };
    const rec = (r: RawRecommendation) => {
      const product = view(r.product);
      const checkout = checkoutFor(product, r.verification_status, catalog);
      record.products.set(product.id, { view: product, checkout });
      return {
        product, checkout, verification_status: cleanText(r.verification_status, 40),
        missing_verifications: (r.missing_verifications ?? []).map(v => cleanText(v, 100)).slice(0, 10),
        rank_reason: cleanText(r.rank_reason, 400),
      };
    };
    const ranked = (result.ranked_recommendations ?? []).map(rec);
    const best = result.recommended_product ? rec(result.recommended_product) : null;
    const unverified = (result.needs_verification ?? []).map(n => ({
      product: view(n.product), reasons: n.reasons.map(r => cleanText(r, 200)).slice(0, 10),
    }));
    saveRecommendation(record);
    return shopJson({
      recommendation_id: record.id,
      intent: result.intent,
      explanation: cleanText(result.explanation, 600),
      best, ranked, needs_verification: unverified,
      trace: (result.trace ?? []).slice(0, 20).map(t => ({
        tool: cleanText(t.tool, 60), status: cleanText(t.status, 40), result_count: Number(t.result_count) || 0,
      })),
    });
  } catch (error) {
    return shopFail(error);
  }
}
