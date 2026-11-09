import type { NextRequest } from "next/server";

const allowedGet = /^\/(?:disputes(?:\/\d+|\/sync\/history)?|paypal\/status|health|evidence\/[A-Za-z0-9_-]{1,255}(?:\/(?:timeline|verification|issues))?|counsel\/[A-Za-z0-9_-]{1,255}(?:\/history)?)$/;
const evidenceAssemble = /^\/evidence\/[A-Za-z0-9_-]{1,255}\/assemble$/;
const counselAnalyze = /^\/counsel\/[A-Za-z0-9_-]{1,255}\/analyze$/;

function error(status: number, message: string) {
  return Response.json({ error: { message } }, {
    status, headers: { "Cache-Control": "no-store" },
  });
}

async function proxy(request: NextRequest, context: RouteContext<"/api/[...segments]">) {
  const { segments } = await context.params;
  const path = `/${segments.join("/")}`;
  if (request.method === "POST") {
    const sync = path === "/disputes/sync";
    const counsel = counselAnalyze.test(path);
    if (!sync && !counsel && !evidenceAssemble.test(path)) return error(404, "Endpoint not found.");
    const origin = request.headers.get("origin");
    const fetchSite = request.headers.get("sec-fetch-site");
    // Next can normalize its internal URL to localhost when the browser uses 127.0.0.1.
    const host = request.headers.get("host") ?? request.nextUrl.host;
    const expectedOrigin = `${request.nextUrl.protocol}//${host}`;
    if (fetchSite ? fetchSite !== "same-origin" : origin && origin !== expectedOrigin) {
      return error(403, sync ? "Dispute sync must be requested from the Gawah console."
        : counsel ? "Counsel analysis must be requested from the Gawah console."
        : "Evidence assembly must be requested from the Gawah console.");
    }
  } else if (!allowedGet.test(path)) {
    return error(404, "Endpoint not found.");
  }

  const key = process.env.SYSTEM3_KEY;
  if (!key && path !== "/health") return error(503, "The System 3 service key is missing.");
  const base = process.env.SYSTEM3_API_URL ?? "http://127.0.0.1:8002";
  try {
    const upstream = await fetch(new URL(`/api${path}${request.nextUrl.search}`, base), {
      method: request.method,
      headers: key ? { "X-Gawah-Key": key } : undefined,
      cache: "no-store",
      redirect: "error",
      signal: request.method === "GET" ? AbortSignal.timeout(35_000) : undefined,
    });
    return new Response(upstream.body, {
      status: upstream.status,
      headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    });
  } catch {
    return error(502, "The System 3 backend could not be reached.");
  }
}

export const GET = proxy;
export const POST = proxy;
