import type { NextRequest } from "next/server";
import { checkoutBlockers, engineHealth, requestHost, shopFail, shopJson } from "@/lib/shop-server";

// What the AI Shopper page needs to know before it offers anything. No keys, only yes/no and labels.
export async function GET(request: NextRequest) {
  try {
    const blockers = checkoutBlockers(requestHost(request));
    let engine: { reachable: boolean; env?: string; simulated?: boolean; sandbox?: boolean; card_configured?: boolean } = { reachable: false };
    // Report the engine's real state even when checkout is disabled (the health endpoint needs no key).
    try {
      const health = await engineHealth();
      engine = {
        reachable: health.status === "ok", env: health.env,
        // the engine's mock mode and its fake PayPal (GAWAH_ENV=test) both mean no real Sandbox payment
        simulated: health.mock === true || health.env === "test",
        sandbox: health.paypal === "sandbox", card_configured: health.card_configured,
      };
    } catch { /* shown as unreachable */ }
    return shopJson({ enabled: blockers.length === 0, blockers, engine });
  } catch (error) {
    return shopFail(error);
  }
}
