import type { NextRequest } from "next/server";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { POST as recommend } from "@/app/api/shop/recommend/route";
import { __resetShopStore } from "@/lib/shop-server";

const raw = (id: string, merchant = "demo-sports", price = "59.99") => ({
  id, title: "Nike SwiftRun 12", brand: "Nike", category: "running shoes", merchant_id: merchant, price, currency: "USD",
  attributes: { color: "black", size: "12" },
});
const rec = (id: string, status: string, merchant?: string, price?: string) => ({
  product: raw(id, merchant, price), verification_status: status, missing_verifications: [], rank_reason: "r",
});
const catalog = [{ product_id: "shoe-001", sku: "s", title: "t", merchant_id: "demo-sports", unit_price_minor: 5999, currency: "USD", stock: 8, variant: "size 12" }];

function post() {
  return {
    method: "POST", nextUrl: new URL("http://localhost:3000/api/shop/recommend"),
    headers: new Headers({ host: "localhost:3000", origin: "http://localhost:3000", "content-type": "application/json" }),
    text: async () => JSON.stringify({ message: "black nike shoes size 12" }),
  } as unknown as NextRequest;
}
async function run(ranked: unknown[], cat: unknown[] = catalog) {
  vi.stubGlobal("fetch", vi.fn(async (url: URL) => url.pathname === "/shopping/agent"
    ? Response.json({ intent: {}, selected_product: null, explanation: "e", alternatives: [], needs_verification: [],
      recommended_product: ranked[0] ?? null, ranked_recommendations: ranked, trace: [] })
    : Response.json(cat)));
  return (await (await recommend(post())).json()).ranked as { product: { id: string }; checkout: { eligible: boolean; reason: string } }[];
}

beforeEach(() => {
  __resetShopStore();
  vi.stubEnv("NODE_ENV", "development"); vi.stubEnv("ENABLE_SHOPPING_CHECKOUT", "true");
  vi.stubEnv("SYSTEM1_KEY", "k1"); vi.stubEnv("SYSTEM3_KEY", "k3");
});
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); });

it("makes a verified demo-catalog product eligible, using the merchant's price and stock", async () => {
  const [r] = await run([rec("shoe-001", "eligible")]);
  expect(r.checkout).toMatchObject({ eligible: true, unit_price_minor: 5999, stock: 8 });
});
it("never makes a product eligible unless the shopper marked it eligible", async () => {
  const [r] = await run([rec("shoe-001", "needs_verification")]);
  expect(r.checkout.eligible).toBe(false);
});
it("never makes a non-catalog (e.g. Channel3) product eligible", async () => {
  const [r] = await run([rec("ch3-abc", "eligible", "some-store")]);
  expect(r.checkout.eligible).toBe(false);
  expect(r.checkout.reason).toMatch(/controlled demo merchant/);
});
it("rejects a listing whose price disagrees with the merchant's", async () => {
  const [r] = await run([rec("shoe-001", "eligible", "demo-sports", "49.99")]);
  expect(r.checkout.eligible).toBe(false);
});
it("rejects out-of-stock products", async () => {
  const [r] = await run([rec("shoe-001", "eligible")], [{ ...catalog[0], stock: 0 }]);
  expect(r.checkout.eligible).toBe(false);
});
