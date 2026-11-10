import type { NextRequest } from "next/server";

const allowedGet = /^\/(?:disputes(?:\/\d+|\/sync\/history)?|paypal\/status|health|evidence\/[A-Za-z0-9_-]{1,255}(?:\/(?:timeline|verification|issues))?|counsel\/[A-Za-z0-9_-]{1,255}(?:\/history)?)$/;
const evidenceAssemble = /^\/evidence\/[A-Za-z0-9_-]{1,255}\/assemble$/;
const counselAnalyze = /^\/counsel\/[A-Za-z0-9_-]{1,255}\/analyze$/;
const dashboardGet = /^\/(?:dashboard\/(?:overview|cases|evidence|counsel|activity|resolution|payments|blocked-purchases|engine-overview)|mandates(?:\/[A-Za-z0-9_-]{1,255}(?:\/budget)?)?|ledger(?:\/(?:head|verify|stream|payments\/[A-Za-z0-9_-]{1,255}\/verification))?|disputes\/\d+\/resolution)$/;
const dashboardDemo = /^\/dashboard\/demo-scenarios$/;
const resolutionGet = /^\/resolution\/(?:actor|overview|workflows(?:\/\d+(?:\/options)?)?)$/;
const resolutionPost = /^\/resolution\/workflows(?:\/\d+\/(?:review|submit|reconcile))?$/;

function error(status: number, message: string) {
  return Response.json({ error: { message } }, {
    status, headers: { "Cache-Control": "no-store" },
  });
}

async function proxy(request: NextRequest, context: RouteContext<"/api/[...segments]">) {
  const { segments } = await context.params;
  const path = `/${segments.join("/")}`;
  const stream = path === "/ledger/stream";
  const resolution = request.method === "POST" ? resolutionPost.test(path) : resolutionGet.test(path);
  let body: string | undefined;
  if (request.method === "POST") {
    const sync = path === "/disputes/sync";
    const counsel = counselAnalyze.test(path);
    if (!sync && !counsel && !resolution && !evidenceAssemble.test(path) && !dashboardDemo.test(path)) {
      return error(404, "Endpoint not found.");
    }
    const origin = request.headers.get("origin");
    const fetchSite = request.headers.get("sec-fetch-site");
    // Next can normalize its internal URL to localhost when the browser uses 127.0.0.1.
    const host = request.headers.get("host") ?? request.nextUrl.host;
    const expectedOrigin = `${request.nextUrl.protocol}//${host}`;
    if (resolution) {
      if (process.env.NODE_ENV !== "development" || !/^(?:localhost|127\.0\.0\.1)(?::\d+)?$/.test(host)
          || origin !== expectedOrigin || (fetchSite && fetchSite !== "same-origin")) {
        return error(403, "Resolution actions require the local Gawah console.");
      }
      if (request.headers.get("content-type")?.split(";")[0] !== "application/json") {
        return error(415, "Resolution actions require a JSON body.");
      }
      body = await request.text();
      if (body.length > 16_000) return error(413, "The resolution request is too large.");
    } else if (fetchSite ? fetchSite !== "same-origin" : origin && origin !== expectedOrigin) {
      return error(403, sync ? "Dispute sync must be requested from the Gawah console."
        : counsel ? "Counsel analysis must be requested from the Gawah console."
        : dashboardDemo.test(path) ? "Demo scenarios must be requested from the Gawah console."
        : "Evidence assembly must be requested from the Gawah console.");
    }
  } else if (!resolution && !allowedGet.test(path) && !dashboardGet.test(path)) {
    return error(404, "Endpoint not found.");
  }

  const key = process.env.SYSTEM3_KEY;
  if (!key && path !== "/health") return error(503, "The System 3 service key is missing.");
  const base = process.env.SYSTEM3_API_URL ?? "http://127.0.0.1:8002";
  const headers: Record<string, string> = key ? { "X-Gawah-Key": key } : {};
  if (resolution) {
    const credential = request.headers.get("authorization");
    if (!credential || !/^Bearer [A-Za-z0-9_-]{1,64}\.[A-Za-z0-9_-]{32,128}$/i.test(credential)) {
      return error(401, "A valid Resolution Center credential is required.");
    }
    headers.Authorization = credential;
  }
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const lastEvent = request.headers.get("last-event-id");
  if (stream && lastEvent) headers["Last-Event-ID"] = lastEvent;
  try {
    const upstream = await fetch(new URL(`/api${path}${request.nextUrl.search}`, base), {
      method: request.method,
      headers,
      body,
      cache: "no-store",
      redirect: "error",
      signal: stream ? request.signal : request.method === "GET" ? AbortSignal.timeout(35_000) : undefined,
    });
    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type": stream && upstream.ok ? "text/event-stream" : "application/json",
        "Cache-Control": "no-store",
        ...(stream ? { "X-Accel-Buffering": "no" } : {}),
      },
    });
  } catch {
    return error(502, "The System 3 backend could not be reached.");
  }
}

export const GET = proxy;
export const POST = proxy;
