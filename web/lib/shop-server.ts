// Server-only helpers for the AI Shopper routes (app/api/shop/*). Never import this from a client
// component: it reads SYSTEM1_KEY / SYSTEM3_KEY and holds prepared proposals in server memory.
import type { NextRequest } from "next/server";

export const SHOP_MAX_BODY = 16_000;
const LOCAL_HOST = /^(?:localhost|127\.0\.0\.1)(?::\d+)?$/;

export type Scope = "system1" | "system3";
export type Verdict = "ALLOW" | "BLOCK";

export class ShopError extends Error {
  constructor(public status: number, message: string, public code = "SHOP_ERROR") {
    super(message);
  }
}

export function shopFail(error: unknown) {
  const e = error instanceof ShopError ? error : new ShopError(500, "The AI Shopper request failed.");
  return Response.json({ error: { code: e.code, message: e.message } }, {
    status: e.status, headers: { "Cache-Control": "no-store" },
  });
}

export function shopJson(data: unknown, status = 200) {
  return Response.json(data, { status, headers: { "Cache-Control": "no-store" } });
}

// ---- who may use it -----------------------------------------------------------------------
// The shopping routes can start a (Sandbox) payment, and the dashboard has no login. So they run
// only as an explicit local demo, the same posture as the Resolution Center: development server,
// loopback host, same-origin browser, an opt-in switch, and both service keys on the server.
export function checkoutBlockers(host: string | null, env: NodeJS.ProcessEnv = process.env): string[] {
  const reasons: string[] = [];
  if (env.ENABLE_SHOPPING_CHECKOUT !== "true") reasons.push("ENABLE_SHOPPING_CHECKOUT=true is not set on the web server.");
  if (env.NODE_ENV !== "development") reasons.push("Shopping checkout runs only on the local development server (npm run dev).");
  if (!host || !LOCAL_HOST.test(host)) reasons.push("Shopping checkout is available only on localhost or 127.0.0.1.");
  if (!env.SYSTEM1_KEY) reasons.push("SYSTEM1_KEY is not set on the web server.");
  if (!env.SYSTEM3_KEY) reasons.push("SYSTEM3_KEY is not set on the web server.");
  return reasons;
}

export function requestHost(request: NextRequest) {
  return request.headers.get("host") ?? request.nextUrl.host;
}

/** Guard + parse for every POST: local demo only, same origin, JSON, small. */
export async function guardedJson(request: NextRequest): Promise<Record<string, unknown>> {
  const host = requestHost(request);
  const blockers = checkoutBlockers(host);
  if (blockers.length) throw new ShopError(403, blockers[0], "SHOP_DISABLED");
  const origin = request.headers.get("origin");
  const fetchSite = request.headers.get("sec-fetch-site");
  if (origin !== `${request.nextUrl.protocol}//${host}` || (fetchSite && fetchSite !== "same-origin")) {
    throw new ShopError(403, "Shopping requests must come from the Gawah console.", "SHOP_ORIGIN");
  }
  if (request.headers.get("content-type")?.split(";")[0] !== "application/json") {
    throw new ShopError(415, "Shopping requests require a JSON body.", "SHOP_CONTENT_TYPE");
  }
  const text = await request.text();
  if (text.length > SHOP_MAX_BODY) throw new ShopError(413, "The request is too large.", "SHOP_TOO_LARGE");
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed as Record<string, unknown>;
  } catch { /* fall through */ }
  throw new ShopError(400, "The request body must be a JSON object.", "SHOP_BAD_JSON");
}

/** Guard for the read-only shop calls that still reach privileged services. */
export function guardedGet(request: NextRequest) {
  const host = requestHost(request);
  const blockers = checkoutBlockers(host);
  if (blockers.length) throw new ShopError(403, blockers[0], "SHOP_DISABLED");
  const fetchSite = request.headers.get("sec-fetch-site");
  if (fetchSite && fetchSite !== "same-origin") throw new ShopError(403, "Shopping requests must come from the Gawah console.", "SHOP_ORIGIN");
}

export function guardAndId(request: NextRequest, name: string): string {
  guardedGet(request);
  const value = request.nextUrl.searchParams.get(name) ?? "";
  if (!/^[A-Za-z0-9-]{8,64}$/.test(value)) throw new ShopError(400, "Missing or invalid id.", "SHOP_BAD_ID");
  return value;
}

// ---- money: strings and integer cents only -------------------------------------------------
export function decimalToMinor(value: unknown): number | null {
  if (typeof value !== "string") return null;
  const m = /^(\d{1,7})(?:\.(\d{1,2}))?$/.exec(value.trim());
  if (!m) return null;
  const cents = Number(m[1]) * 100 + Number((m[2] ?? "").padEnd(2, "0") || "0");
  return cents > 0 ? cents : null;
}

export function minorToDecimal(minor: number) {
  return `${Math.floor(minor / 100)}.${String(minor % 100).padStart(2, "0")}`;
}

// ---- upstream calls, with the key scopes spelled out ----------------------------------------
// Only these engine calls exist. There is deliberately no refund, revoke or payment route here,
// and the shopper key (System 1) can only propose; the System 3 key only creates mandates and
// runs the Guardian dry run.
const engineAllowed: Record<Scope, RegExp[]> = {
  system1: [/^POST \/v1\/proposals$/, /^GET \/v1\/merchants\/[A-Za-z0-9_-]{1,128}\/public-key$/],
  system3: [/^POST \/v1\/mandates$/, /^POST \/v1\/guardian\/check$/, /^GET \/v1\/payments\?mandate_id=[A-Za-z0-9_-]{1,128}$/],
};
const shopperAllowed = [/^POST \/shopping\/agent$/, /^GET \/shopping\/demo\/products$/, /^POST \/shopping\/proposals\/prepare$/];

export type Upstream = { status: number; data: unknown };

async function call(base: string, method: string, path: string, headers: Record<string, string>,
  body: unknown, timeoutMs: number, label: string): Promise<Upstream> {
  try {
    const response = await fetch(new URL(path, base), {
      method, headers: { ...headers, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      cache: "no-store", redirect: "error", signal: AbortSignal.timeout(timeoutMs),
    });
    let data: unknown = null;
    try { data = await response.json(); } catch { /* an empty or non-JSON body */ }
    return { status: response.status, data };
  } catch (cause) {
    const timeout = cause instanceof Error && (cause.name === "TimeoutError" || cause.name === "AbortError");
    throw new ShopError(timeout ? 504 : 502,
      timeout ? `${label} did not answer in time.` : `${label} could not be reached.`, timeout ? "UPSTREAM_TIMEOUT" : "UPSTREAM_UNREACHABLE");
  }
}

export function engineBase() { return process.env.ENGINE_URL ?? "http://127.0.0.1:8000"; }
export function shopperBase() { return process.env.SHOPPER_URL ?? "http://127.0.0.1:8001"; }

export function engineCall(scope: Scope, method: string, path: string, body?: unknown, timeoutMs = 30_000) {
  if (!engineAllowed[scope].some(rule => rule.test(`${method} ${path}`))) {
    throw new ShopError(500, "That engine call is not allowed from the shopping routes.", "SHOP_FORBIDDEN_CALL");
  }
  const key = scope === "system1" ? process.env.SYSTEM1_KEY : process.env.SYSTEM3_KEY;
  if (!key) throw new ShopError(503, `The ${scope === "system1" ? "SYSTEM1_KEY" : "SYSTEM3_KEY"} service key is missing.`, "SHOP_KEY_MISSING");
  return call(engineBase(), method, path, { "X-Gawah-Key": key }, body, timeoutMs, "The engine (System 2)");
}

export function shopperCall(method: string, path: string, body?: unknown, timeoutMs = 60_000) {
  if (!shopperAllowed.some(rule => rule.test(`${method} ${path}`))) {
    throw new ShopError(500, "That shopper call is not allowed from the shopping routes.", "SHOP_FORBIDDEN_CALL");
  }
  return call(shopperBase(), method, path, {}, body, timeoutMs, "The shopper (System 1)");
}

export async function engineHealth() {
  const out = await call(engineBase(), "GET", "/health", {}, undefined, 10_000, "The engine (System 2)");
  return (out.data ?? {}) as { status?: string; env?: string; mock?: boolean; paypal?: string; card_configured?: boolean };
}

export function shopperProblem(status: number): ShopError {
  if (status === 429) return new ShopError(429, "The AI service rate limit was reached. Try again in a minute.", "SHOPPER_RATE_LIMIT");
  if (status === 503) return new ShopError(503, "The shopping service is unavailable (is GROQ_API_KEY set for the shopper?).", "SHOPPER_UNAVAILABLE");
  if (status === 502) return new ShopError(502, "The shopping agent returned an invalid response.", "SHOPPER_INVALID");
  if (status === 422) return new ShopError(422, "The demo merchant rejected the request (identity, stock or merchant).", "MERCHANT_REJECTED");
  return new ShopError(502, `The shopper answered with HTTP ${status}.`, "SHOPPER_ERROR");
}

export function engineProblem(status: number, data: unknown): ShopError {
  const error = (data as { error?: { code?: string; message?: string } } | null)?.error;
  const code = error?.code ?? "ENGINE_ERROR";
  const message = typeof error?.message === "string" ? error.message.slice(0, 200) : `The engine answered with HTTP ${status}.`;
  return new ShopError(status >= 500 ? 502 : status, message, code);
}

// ---- server-side memory: what the user was shown, and what may be paid ---------------------
export type ProductView = {
  id: string; title: string; brand: string; category: string; merchant_id: string;
  price: string; currency: string; attributes: Record<string, string>;
};
export type Checkout = { eligible: boolean; reason: string; unit_price_minor: number | null; stock: number | null };
export type RecommendationRecord = {
  id: string; createdAt: number; intent: Record<string, unknown>; products: Map<string, { view: ProductView; checkout: Checkout }>;
  mandateId?: string; mandateBudgetMinor?: number; mandateCurrency?: string; mandateMerchant?: string;
  mandateProductId?: string;
};
export type PreparedRecord = {
  id: string; createdAt: number; recommendationId: string; mandateId: string; proposal: Record<string, unknown>;
  proposalId: string; totalMinor: number; currency: string; merchantId: string; title: string;
  dryRunVerdict: Verdict; state: "prepared" | "submitting" | "done" | "unknown";
  result?: Record<string, unknown>;
};

type Store = { recs: Map<string, RecommendationRecord>; prepared: Map<string, PreparedRecord> };
const g = globalThis as unknown as { __gawahShop?: Store };
function store(): Store {
  g.__gawahShop ??= { recs: new Map(), prepared: new Map() };
  return g.__gawahShop;
}
const TTL_MS = 30 * 60_000;
const MAX_ITEMS = 100;

function prune<T extends { createdAt: number }>(map: Map<string, T>) {
  const now = Date.now();
  for (const [key, value] of map) if (now - value.createdAt > TTL_MS) map.delete(key);
  while (map.size >= MAX_ITEMS) map.delete(map.keys().next().value as string);
}

export function newId() { return crypto.randomUUID(); }

export function saveRecommendation(record: RecommendationRecord) {
  prune(store().recs);
  store().recs.set(record.id, record);
}
export function getRecommendation(id: unknown): RecommendationRecord {
  const record = typeof id === "string" ? store().recs.get(id) : undefined;
  if (!record || Date.now() - record.createdAt > TTL_MS) {
    throw new ShopError(404, "That recommendation has expired. Run the search again.", "RECOMMENDATION_EXPIRED");
  }
  return record;
}

export function savePrepared(record: PreparedRecord) {
  prune(store().prepared);
  store().prepared.set(record.id, record);
}
export function getPrepared(id: unknown): PreparedRecord {
  const record = typeof id === "string" ? store().prepared.get(id) : undefined;
  if (!record || Date.now() - record.createdAt > TTL_MS) {
    throw new ShopError(404, "That prepared proposal has expired. Prepare it again.", "PREPARED_EXPIRED");
  }
  return record;
}

/** Claims a prepared proposal for submission. The state flips before any await, so two clicks, or
 * two tabs, cannot both reach the engine. */
export function beginSubmit(record: PreparedRecord) {
  if (record.state !== "prepared") {
    throw new ShopError(409, record.state === "done"
      ? "This purchase was already submitted. Its result is shown below."
      : "This purchase is already being submitted, or its outcome is unknown. It is not retried automatically.",
    "ALREADY_SUBMITTED");
  }
  record.state = "submitting";
}

export function __resetShopStore() { delete g.__gawahShop; }

// ---- strings that reach the engine ---------------------------------------------------------
export function cleanText(value: unknown, max: number): string {
  return typeof value === "string"
    ? value.replace(/[\u0000-\u001f\u007f​-‏‪-‮⁦-⁩]/g, " ").replace(/\s+/g, " ").trim().slice(0, max)
    : "";
}

/** The mandate's item description, built from the validated intent (never free text from the page). */
export function mandateDescription(intent: Record<string, unknown>, product: ProductView, budgetMinor: number): string {
  const parts = [intent.color, intent.brand, product.category].map(v => cleanText(v, 40)).filter(Boolean);
  const size = cleanText(intent.size ?? product.attributes.size, 20);
  const text = [parts.join(" "), size ? `size ${size}` : "", `under $${minorToDecimal(budgetMinor)}`].filter(Boolean).join(", ");
  return text.slice(0, 480);
}
