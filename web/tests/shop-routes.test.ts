import type { NextRequest } from "next/server";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { POST as confirm } from "@/app/api/shop/confirm/route";
import { GET as config } from "@/app/api/shop/config/route";
import { POST as recommend } from "@/app/api/shop/recommend/route";
import { __resetShopStore, savePrepared, type PreparedRecord } from "@/lib/shop-server";

const ID = "22222222-2222-2222-2222-222222222222";

function post(body: unknown, headers: Record<string, string> = {}) {
  return {
    method: "POST", nextUrl: new URL("http://localhost:3000/api/shop/x"),
    headers: new Headers({ host: "localhost:3000", origin: "http://localhost:3000", "content-type": "application/json", ...headers }),
    text: async () => JSON.stringify(body),
  } as unknown as NextRequest;
}
function prepared(over: Partial<PreparedRecord> = {}): PreparedRecord {
  return {
    id: ID, createdAt: Date.now(), recommendationId: "r", mandateId: "mandate_1", proposal: { id: "prop_1" }, proposalId: "prop_1",
    totalMinor: 5999, currency: "USD", merchantId: "demo", title: "Shoe", dryRunVerdict: "ALLOW", state: "prepared", ...over,
  };
}
const health = { status: "ok", env: "development", mock: false, paypal: "sandbox", card_configured: true };
const result = {
  decision: { id: "d1", verdict: "ALLOW", reasons: [], explanation: "ok" },
  payment: { id: "pay1", status: "PAID", amount_minor: 5999, currency: "USD", paypal_order_id: "ORD", paypal_capture_id: "CAP",
    failure_reason: null, created_at: "2026-01-01T00:00:00Z", idempotency_key: "secret-idem", custom_id: "x" },
};
const body = { prepared_id: ID, confirmed: true, expected_total_minor: 5999, expected_currency: "USD" };

/** Fake fetch: /health and /v1/proposals only. Counts proposal calls; no network is touched. */
function fakeEngine(proposal: () => Response | Promise<Response>) {
  const proposals = vi.fn(proposal);
  vi.stubGlobal("fetch", vi.fn(async (url: URL) => {
    if (url.pathname === "/health") return Response.json(health);
    if (url.pathname === "/v1/proposals") return proposals();
    throw new Error(`unexpected call ${url.pathname}`);
  }));
  return proposals;
}

beforeEach(() => {
  __resetShopStore();
  vi.stubEnv("NODE_ENV", "development"); vi.stubEnv("ENABLE_SHOPPING_CHECKOUT", "true");
  vi.stubEnv("SYSTEM1_KEY", "k1"); vi.stubEnv("SYSTEM3_KEY", "k3");
});
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); });

it("refuses everything when checkout is not enabled, without contacting any service", async () => {
  vi.stubEnv("ENABLE_SHOPPING_CHECKOUT", "");
  const fetchMock = vi.fn(); vi.stubGlobal("fetch", fetchMock);
  expect((await recommend(post({ message: "shoes" }))).status).toBe(403);
  expect((await confirm(post(body))).status).toBe(403);
  expect(fetchMock).not.toHaveBeenCalled();
});

it("rejects cross-origin and non-JSON posts", async () => {
  savePrepared(prepared());
  const fetchMock = vi.fn(); vi.stubGlobal("fetch", fetchMock);
  expect((await confirm(post(body, { origin: "http://evil.test" }))).status).toBe(403);
  expect((await confirm(post(body, { "content-type": "text/plain" }))).status).toBe(415);
  expect(fetchMock).not.toHaveBeenCalled();
});

it("requires explicit confirmation and a matching amount before any submission", async () => {
  savePrepared(prepared());
  const proposals = fakeEngine(() => Response.json(result));
  expect((await confirm(post({ ...body, confirmed: false }))).status).toBe(422);
  expect((await confirm(post({ ...body, expected_total_minor: 1 }))).status).toBe(409);
  expect(proposals).not.toHaveBeenCalled();
});

it("will not submit a proposal whose Guardian dry run did not ALLOW", async () => {
  savePrepared(prepared({ dryRunVerdict: "BLOCK" }));
  const proposals = fakeEngine(() => Response.json(result));
  expect((await confirm(post(body))).status).toBe(409);
  expect(proposals).not.toHaveBeenCalled();
});

it("will not submit when the engine does not report a PayPal sandbox", async () => {
  savePrepared(prepared());
  const proposals = vi.fn();
  vi.stubGlobal("fetch", vi.fn(async (url: URL) => url.pathname === "/health" ? Response.json({ ...health, paypal: "live" }) : proposals()));
  const response = await confirm(post(body));
  expect(response.status).toBe(409);
  expect(proposals).not.toHaveBeenCalled();
});

it("submits the server-held proposal once, returns safe payment fields, and treats a second confirm as a duplicate", async () => {
  savePrepared(prepared());
  const proposals = fakeEngine(() => Response.json(result));
  const first = await confirm(post(body));
  const data = await first.json();
  expect(first.status).toBe(200);
  expect(data.payment).toMatchObject({ status: "PAID", paypal_order_id: "ORD", paypal_capture_id: "CAP" });
  expect(JSON.stringify(data)).not.toMatch(/secret-idem|custom_id|idempotency/);
  const second = await confirm(post(body));
  expect((await second.json()).duplicate).toBe(true);
  expect(proposals).toHaveBeenCalledTimes(1);
});

it("two concurrent confirms reach the engine only once", async () => {
  savePrepared(prepared());
  const proposals = fakeEngine(async () => { await new Promise(r => setTimeout(r, 20)); return Response.json(result); });
  const [a, b] = await Promise.all([confirm(post(body)), confirm(post(body))]);
  expect([a.status, b.status].sort()).toEqual([200, 409]);
  expect(proposals).toHaveBeenCalledTimes(1);
});

it("reports an unknown outcome on a server error and never retries or allows a resubmit", async () => {
  savePrepared(prepared());
  const proposals = fakeEngine(() => new Response("boom", { status: 503 }));
  const first = await confirm(post(body));
  expect(first.status).toBe(202);
  expect((await first.json()).outcome).toBe("UNKNOWN");
  expect((await confirm(post(body))).status).toBe(409);
  expect(proposals).toHaveBeenCalledTimes(1);
});

it("labels payments simulated when the engine is in mock mode", async () => {
  savePrepared(prepared());
  vi.stubGlobal("fetch", vi.fn(async (url: URL) => url.pathname === "/health"
    ? Response.json({ ...health, mock: true, card_configured: false }) : Response.json(result)));
  expect((await (await confirm(post(body))).json()).simulated).toBe(true);
});

it("config reports blockers and never includes keys", async () => {
  vi.stubEnv("ENABLE_SHOPPING_CHECKOUT", "");
  vi.stubGlobal("fetch", vi.fn(async () => Response.json(health)));
  const response = await config({ headers: new Headers({ host: "localhost:3000" }), nextUrl: new URL("http://localhost:3000/api/shop/config") } as unknown as NextRequest);
  const text = JSON.stringify(await response.json());
  expect(text).toContain("ENABLE_SHOPPING_CHECKOUT");
  expect(text).not.toMatch(/k1|k3|X-Gawah-Key/);
});

const cfgRequest = () => ({ headers: new Headers({ host: "localhost:3000" }), nextUrl: new URL("http://localhost:3000/api/shop/config") } as unknown as NextRequest);

it("config reports the engine's real state even when checkout is disabled", async () => {
  vi.stubEnv("ENABLE_SHOPPING_CHECKOUT", "");
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ...health, env: "test", mock: false })));
  const data = await (await config(cfgRequest())).json();
  expect(data.enabled).toBe(false);
  expect(data.engine).toMatchObject({ reachable: true, simulated: true, sandbox: true });
});

it("config says unreachable only when the engine really does not answer", async () => {
  vi.stubEnv("ENABLE_SHOPPING_CHECKOUT", "");
  vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("down"); }));
  expect((await (await config(cfgRequest())).json()).engine.reachable).toBe(false);
});
