import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import ShopPage from "@/app/shop/page";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const product = (id: string, title: string) => ({
  id, title, brand: "Nike", category: "running shoes", merchant_id: "demo-merchant", price: "59.99", currency: "USD",
  attributes: { color: "black", size: "12" },
});
const config = { enabled: true, blockers: [], engine: { reachable: true, simulated: false, sandbox: true } };
const recommendation = {
  recommendation_id: "r1", intent: { category: "running shoes", color: "black" }, explanation: "Best fit under budget.",
  best: { product: product("p1", "Nike Pegasus"), checkout: { eligible: true, reason: "Controlled demo merchant: signed cart available.", unit_price_minor: 5999, stock: 3 },
    verification_status: "eligible", missing_verifications: [], rank_reason: "Matches colour, size and price." },
  ranked: [{ product: product("p2", "External Runner"), checkout: { eligible: false, reason: "Not sold by the controlled demo merchant.", unit_price_minor: null, stock: null },
    verification_status: "needs_verification", missing_verifications: ["stock"], rank_reason: "Cheaper but unverified." }],
  needs_verification: [], trace: [{ tool: "search", status: "ok", result_count: 2 }],
};

function mount() {
  const fetchMock = vi.fn(async (url: string) => Response.json(url.endsWith("/config") ? config : recommendation));
  vi.stubGlobal("fetch", fetchMock);
  render(<QueryClientProvider client={new QueryClient()}><ShopPage /></QueryClientProvider>);
  return fetchMock;
}

it("shows recommendations, highlights the best match and blocks ineligible products", async () => {
  mount();
  fireEvent.click(screen.getByRole("button", { name: /find products/i }));
  expect(await screen.findByText("Nike Pegasus")).toBeTruthy();
  expect(screen.getByText("Best match")).toBeTruthy();
  const ineligible = screen.getByRole("article", { name: "External Runner" });
  expect((ineligible.querySelector("button") as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText(/Not sold by the controlled demo merchant/)).toBeTruthy();
});

it("requires the approval checkbox before a mandate can be created", async () => {
  mount();
  fireEvent.click(screen.getByRole("button", { name: /find products/i }));
  fireEvent.click((await screen.findAllByRole("button", { name: /select this product/i }))[0]);
  const create = await screen.findByRole("button", { name: /create and sign mandate/i }) as HTMLButtonElement;
  expect(create.disabled).toBe(true);
  fireEvent.click(screen.getByRole("checkbox"));
  await waitFor(() => expect(create.disabled).toBe(false));
});

it("explains why checkout is disabled", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ...config, enabled: false, blockers: ["ENABLE_SHOPPING_CHECKOUT=true is not set on the web server."] })));
  render(<QueryClientProvider client={new QueryClient()}><ShopPage /></QueryClientProvider>);
  expect(await screen.findByText(/Checkout is disabled/)).toBeTruthy();
  expect((screen.getByRole("button", { name: /find products/i }) as HTMLButtonElement).disabled).toBe(true);
});

it("always shows the local-demo banner and reports the engine state when checkout is disabled", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ enabled: false, blockers: ["ENABLE_SHOPPING_CHECKOUT=true is not set on the web server."],
    engine: { reachable: true, simulated: false, sandbox: true, card_configured: true } })));
  render(<QueryClientProvider client={new QueryClient()}><ShopPage /></QueryClientProvider>);
  expect(await screen.findByText("Local demo page: needs the shopper agent running.")).toBeTruthy();
  expect(await screen.findByText(/Engine reachable: PayPal Sandbox/)).toBeTruthy();
  expect(screen.queryByText(/did not answer its health check/)).toBeNull();
});

it("labels the evidence panel simulated or sandbox from the engine's own state", async () => {
  const { EvidencePanel } = await import("@/components/shop/evidence-panel");
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({})));
  for (const [simulated, want] of [[true, /SIMULATED/], [false, /PayPal sandbox/]] as const) {
    const view = render(<QueryClientProvider client={new QueryClient()}><EvidencePanel mandateId="m" paymentId={null} simulated={simulated} /></QueryClientProvider>);
    expect(view.container.textContent).toMatch(want);
    expect(view.container.textContent).not.toMatch(/real Sandbox/);
    view.unmount();
  }
});
