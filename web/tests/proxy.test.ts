import type { NextRequest } from "next/server";
import { afterEach, expect, it, vi } from "vitest";
import { POST } from "@/app/api/[...segments]/route";

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
