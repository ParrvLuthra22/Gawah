import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ShopError, __resetShopStore, beginSubmit, checkoutBlockers, decimalToMinor, engineCall, getPrepared, minorToDecimal,
  savePrepared, shopperCall, type PreparedRecord,
} from "@/lib/shop-server";

const ok = { ENABLE_SHOPPING_CHECKOUT: "true", NODE_ENV: "development", SYSTEM1_KEY: "a", SYSTEM3_KEY: "b" } as unknown as NodeJS.ProcessEnv;

function record(over: Partial<PreparedRecord> = {}): PreparedRecord {
  return {
    id: "11111111-1111-1111-1111-111111111111", createdAt: Date.now(), recommendationId: "r", mandateId: "m", proposal: { id: "p" },
    proposalId: "p", totalMinor: 5999, currency: "USD", merchantId: "demo", title: "Shoe", dryRunVerdict: "ALLOW", state: "prepared", ...over,
  };
}

beforeEach(() => __resetShopStore());
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); });

describe("checkout guard", () => {
  it("allows only the opted-in local dev server", () => {
    expect(checkoutBlockers("localhost:3000", ok)).toEqual([]);
    expect(checkoutBlockers("127.0.0.1:3000", ok)).toEqual([]);
  });
  it.each([
    ["not opted in", { ...ok, ENABLE_SHOPPING_CHECKOUT: undefined }, "localhost:3000"],
    ["production", { ...ok, NODE_ENV: "production" }, "localhost:3000"],
    ["public host", ok, "gawah.onrender.com"],
    ["no system1 key", { ...ok, SYSTEM1_KEY: "" }, "localhost:3000"],
    ["no system3 key", { ...ok, SYSTEM3_KEY: "" }, "localhost:3000"],
  ])("blocks: %s", (_n, env, host) => {
    expect(checkoutBlockers(host, env as NodeJS.ProcessEnv).length).toBeGreaterThan(0);
  });
});

describe("money", () => {
  it("parses decimal strings to integer cents and rejects floats and junk", () => {
    expect(decimalToMinor("59.99")).toBe(5999);
    expect(decimalToMinor("60")).toBe(6000);
    expect(decimalToMinor("0.5")).toBe(50);
    expect(decimalToMinor(59.99)).toBeNull();
    for (const bad of ["-1", "1e3", "1.234", "0", "", "abc"]) expect(decimalToMinor(bad)).toBeNull();
    expect(minorToDecimal(5905)).toBe("59.05");
  });
});

describe("upstream allowlists", () => {
  it("has no refund, revoke or payment-creation route, whichever key is used", () => {
    vi.stubEnv("SYSTEM1_KEY", "k1"); vi.stubEnv("SYSTEM3_KEY", "k3");
    for (const [scope, method, path] of [
      ["system1", "POST", "/v1/payments/x/refund"], ["system3", "POST", "/v1/payments/x/refund"],
      ["system3", "POST", "/v1/proposals"], ["system1", "POST", "/v1/mandates"], ["system1", "POST", "/v1/guardian/check"],
      ["system3", "DELETE", "/v1/mandates/x"], ["system3", "GET", "/v1/ledger/verify"],
    ] as const) {
      expect(() => engineCall(scope, method, path)).toThrow(ShopError);
    }
    expect(() => shopperCall("POST", "/shopping/refund")).toThrow(ShopError);
  });
  it("sends each scope's own key as a header and nothing else", async () => {
    vi.stubEnv("SYSTEM1_KEY", "k1"); vi.stubEnv("SYSTEM3_KEY", "k3");
    const fetchMock = vi.fn().mockResolvedValue(Response.json({}));
    vi.stubGlobal("fetch", fetchMock);
    await engineCall("system1", "POST", "/v1/proposals", { a: 1 });
    await engineCall("system3", "POST", "/v1/guardian/check", { a: 1 });
    expect(fetchMock.mock.calls[0][1].headers["X-Gawah-Key"]).toBe("k1");
    expect(fetchMock.mock.calls[1][1].headers["X-Gawah-Key"]).toBe("k3");
  });
  it("does not leak the upstream URL or key in a connection error", async () => {
    vi.stubEnv("SYSTEM1_KEY", "super-secret-key");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("connect ECONNREFUSED 10.0.0.5 super-secret-key")));
    const error = await engineCall("system1", "POST", "/v1/proposals", {}).catch(e => e as ShopError) as ShopError;
    expect(String(error.message)).not.toMatch(/super-secret|10\.0\.0\.5/);
  });
});

describe("prepared store and one-shot submit", () => {
  it("lets exactly one caller claim a proposal", () => {
    savePrepared(record());
    const r = getPrepared("11111111-1111-1111-1111-111111111111");
    beginSubmit(r);
    expect(() => beginSubmit(r)).toThrow(/already/);
  });
  it("stays claimed when the outcome is unknown (no automatic retry)", () => {
    const r = record({ state: "unknown" });
    expect(() => beginSubmit(r)).toThrow(ShopError);
  });
  it("expires old entries", () => {
    savePrepared(record({ createdAt: Date.now() - 31 * 60_000 }));
    expect(() => getPrepared("11111111-1111-1111-1111-111111111111")).toThrow(/expired/);
  });
});
