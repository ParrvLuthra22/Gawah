import type { NextRequest } from "next/server";
import { afterEach, expect, it, vi } from "vitest";
import { GET, POST } from "@/app/api/[...segments]/route";

const context = { params: Promise.resolve({ segments: ["disputes", "sync"] }) };

function request(headers: Record<string, string>) {
  return {
    method: "POST",
    nextUrl: new URL("http://localhost:3000/api/disputes/sync"),
    headers: new Headers({ host: "127.0.0.1:3000", ...headers }),
  } as NextRequest;
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

it("accepts the browser origin when Next normalizes its internal loopback URL", async () => {
  vi.stubEnv("SYSTEM3_KEY", "server-key");
  vi.stubEnv("SYSTEM3_API_URL", "http://127.0.0.1:8002");
  const upstream = vi.fn().mockResolvedValue(Response.json({ result: "SUCCESS" }));
  vi.stubGlobal("fetch", upstream);
  const response = await POST(request({ origin: "http://127.0.0.1:3000" }), context);
  expect(response.status).toBe(200);
  expect(upstream).toHaveBeenCalledWith(
    new URL("http://127.0.0.1:8002/api/disputes/sync"),
    expect.objectContaining({ method: "POST", headers: { "X-Gawah-Key": "server-key" } }),
  );
  expect(response.headers.has("X-Gawah-Key")).toBe(false);
});

const crossOriginHeaders: Record<string, string>[] = [
  { origin: "http://other.test" },
  { "sec-fetch-site": "cross-site" },
];

it.each(crossOriginHeaders)("rejects a cross-origin browser sync before contacting FastAPI", async headers => {
  const upstream = vi.fn();
  vi.stubGlobal("fetch", upstream);
  const response = await POST(request(headers), context);
  expect(response.status).toBe(403);
  expect(upstream).not.toHaveBeenCalled();
});

it("passes ledger events and the resume cursor without exposing the service key", async () => {
  vi.stubEnv("SYSTEM3_KEY", "server-key");
  const body = "id: 9\nevent: ledger\ndata: {}\n\n";
  const upstream = vi.fn().mockResolvedValue(new Response(body));
  vi.stubGlobal("fetch", upstream);
  const response = await GET({
    method: "GET",
    nextUrl: new URL("http://127.0.0.1:3000/api/ledger/stream?since_seq=4"),
    headers: new Headers({ "last-event-id": "8", "X-Gawah-Key": "untrusted" }),
  } as NextRequest, { params: Promise.resolve({ segments: ["ledger", "stream"] }) });
  expect(await response.text()).toBe(body);
  expect(response.headers.get("Content-Type")).toBe("text/event-stream");
  expect(response.headers.has("X-Gawah-Key")).toBe(false);
  expect(upstream).toHaveBeenCalledWith(expect.any(URL), expect.objectContaining({
    headers: { "X-Gawah-Key": "server-key", "Last-Event-ID": "8" },
  }));
});

it("rejects resolution writes on hosted consoles and across origins", async () => {
  const action = { params: Promise.resolve({ segments: ["resolution", "workflows", "7", "submit"] }) };
  const upstream = vi.fn();
  vi.stubGlobal("fetch", upstream);
  vi.stubEnv("NODE_ENV", "production");
  expect((await POST(request({ origin: "http://127.0.0.1:3000" }), action)).status).toBe(403);
  vi.stubEnv("NODE_ENV", "development");
  expect((await POST(request({ origin: "http://other.test" }), action)).status).toBe(403);
  expect((await POST(request({ host: "other.test", origin: "http://other.test" }), action)).status).toBe(403);
  expect(upstream).not.toHaveBeenCalled();
});

it("forwards reviewed JSON for an allowlisted action from the local console", async () => {
  vi.stubEnv("NODE_ENV", "development");
  vi.stubEnv("SYSTEM3_KEY", "server-key");
  const upstream = vi.fn().mockResolvedValue(Response.json({ status: "SUCCEEDED" }));
  vi.stubGlobal("fetch", upstream);
  const body = '{"confirmation":"PP-D-1","revision":3}';
  const authorization = "Bearer navya." + "x".repeat(40);
  const input = Object.assign(
    request({ origin: "http://127.0.0.1:3000", "content-type": "application/json", authorization }),
    { text: async () => body },
  );
  const response = await POST(input, {
    params: Promise.resolve({ segments: ["resolution", "workflows", "7", "submit"] }),
  });
  expect(response.status).toBe(200);
  expect(upstream).toHaveBeenCalledWith(expect.any(URL), expect.objectContaining({
    method: "POST", body, headers: {
      "X-Gawah-Key": "server-key", "Content-Type": "application/json", Authorization: authorization,
    },
  }));
});

it("blocks the former direct execution route and requires an operator credential for workflow reads", async () => {
  vi.stubEnv("SYSTEM3_KEY", "server-key");
  const upstream = vi.fn();
  vi.stubGlobal("fetch", upstream);
  const input = request({ origin: "http://127.0.0.1:3000" });
  expect((await POST(input, {
    params: Promise.resolve({ segments: ["disputes", "7", "resolution", "refund"] }),
  })).status).toBe(404);
  expect((await GET({ ...input, method: "GET" } as NextRequest, {
    params: Promise.resolve({ segments: ["resolution", "workflows"] }),
  })).status).toBe(401);
  expect(upstream).not.toHaveBeenCalled();
});

it("allows known dashboard reads with the service key and rejects dashboard writes", async () => {
  vi.stubEnv("SYSTEM3_KEY", "server-key");
  const upstream = vi.fn().mockResolvedValue(Response.json({ total: 2 }));
  vi.stubGlobal("fetch", upstream);
  const nextUrl = new URL("http://127.0.0.1:3000/api/dashboard/overview?source=PAYPAL_API");
  const context = { params: Promise.resolve({ segments: ["dashboard", "overview"] }) };
  const response = await GET({ method: "GET", nextUrl, headers: new Headers() } as NextRequest, context);
  expect(await response.json()).toEqual({ total: 2 });
  expect(upstream).toHaveBeenCalledWith(expect.any(URL), expect.objectContaining({
    headers: { "X-Gawah-Key": "server-key" },
  }));
  expect(response.headers.has("X-Gawah-Key")).toBe(false);
  expect((await POST(request({}), context)).status).toBe(404);
  expect((await GET({ method: "GET", nextUrl, headers: new Headers() } as NextRequest, {
    params: Promise.resolve({ segments: ["dashboard", "private"] }),
  })).status).toBe(404);
  expect(upstream).toHaveBeenCalledTimes(1);
});
