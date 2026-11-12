// Client-side types and helpers for the AI Shopper. Every call goes to /api/shop/*; the browser
// never holds service keys, carts or proposals, only opaque ids returned by the server.
export type Product = {
  id: string; title: string; brand: string; category: string; merchant_id: string;
  price: string; currency: string; attributes: Record<string, string>;
};
export type Checkout = { eligible: boolean; reason: string; unit_price_minor: number | null; stock: number | null };
export type Recommendation = {
  product: Product; checkout: Checkout; verification_status: string;
  missing_verifications: string[]; rank_reason: string;
};
export type RecommendResult = {
  recommendation_id: string; intent: Record<string, unknown>; explanation: string;
  best: Recommendation | null; ranked: Recommendation[];
  needs_verification: { product: Product; reasons: string[] }[];
  trace: { tool: string; status: string; result_count: number }[];
};
export type Config = {
  enabled: boolean; blockers: string[];
  engine: { reachable: boolean; env?: string; simulated?: boolean; sandbox?: boolean; card_configured?: boolean };
};
export type MandateSummary = {
  id: string; category: string; item_description: string; max_amount_minor: number; currency: string;
  allowed_merchants: string[]; issued_at: string; expires_at: string; status: string; signing_alg: string; key_id: string;
};
export type Guardian = { dry_run: boolean; id: string; verdict: "ALLOW" | "BLOCK"; reasons: string[]; explanation: string };
export type Prepared = {
  prepared_id: string; merchant_key_registered: boolean; guardian: Guardian;
  proposal: Record<string, unknown> & { total_minor?: number; currency?: string };
};
export type PaymentView = {
  id: string; status: string; amount_minor: number; currency: string; paypal_order_id: string | null;
  paypal_capture_id: string | null; failure_reason: string | null; created_at?: string;
};
export type ConfirmResult = {
  outcome: string; simulated?: boolean; duplicate?: boolean; message?: string;
  decision?: { id: string; verdict: string; reasons: string[]; explanation: string };
  payment?: PaymentView | null;
};

export const examplePrompts = [
  "Find me black Nike running shoes, size 12, under $60.",
  "I need white running shoes in size 10 under $100.",
  "Wireless noise cancelling headphones under $150.",
];

export async function shopApi<T>(path: string, init?: { method?: "GET" | "POST"; body?: unknown; signal?: AbortSignal }): Promise<T> {
  const response = await fetch(`/api/shop/${path}`, {
    method: init?.method ?? "GET", signal: init?.signal, cache: "no-store",
    ...(init?.body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(init.body) }),
  });
  let data: { error?: { message?: string } } & Record<string, unknown> = {};
  try { data = await response.json(); } catch { /* non-JSON error body */ }
  if (!response.ok && response.status !== 202) throw new Error(data.error?.message ?? "The request failed.");
  return data as T;
}
