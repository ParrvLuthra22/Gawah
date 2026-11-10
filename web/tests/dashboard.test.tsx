import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import LedgerPage from "@/app/ledger/page";
import MandatesPage from "@/app/mandates/page";

const ts = "2026-11-09T12:00:00Z";

function view(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{children}</QueryClientProvider>);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("loads the ledger, deduplicates streamed entries, and shows named verification failures", async () => {
  const head = { seq: 1, message: "message", signature: "SIMULATED-example", key_id: "mock" };
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    const responses: Record<string, unknown> = {
      "/api/ledger": [{ seq: 1, ts, type: "MANDATE_CREATED", subject_id: "man_01" }],
      "/api/ledger/head": head,
      "/api/ledger/verify": { ok: true, checked: 1, first_broken_seq: null, checks: [] },
      "/api/ledger/payments/pay_04/verification": {
        ok: false, checked: 1, first_broken_seq: null,
        checks: [{ name: "cart_signature", ok: false, detail: "Invalid signature." }],
      },
    };
    return Promise.resolve(Response.json(responses[url]));
  }));
  let ledgerEvent: (event: MessageEvent<string>) => void;
  const close = vi.fn();
  const source = vi.fn(function Source() {
    return { addEventListener: (_event: string, cb: typeof ledgerEvent) => { ledgerEvent = cb; }, close };
  });
  vi.stubGlobal("EventSource", source);
  const screenView = view(<LedgerPage />);
  expect(await screen.findByText("man_01")).toBeTruthy();
  expect(source).toHaveBeenCalledWith("/api/ledger/stream?since_seq=1");
  const entry = new MessageEvent("ledger", { data: JSON.stringify({
    seq: 2, ts, type: "PAYMENT_COMPLETED", subject_id: "pay_04",
  }) });
  act(() => { ledgerEvent(entry); ledgerEvent(entry); });
  expect(screen.getAllByText("pay_04")).toHaveLength(1);

  fireEvent.click(screen.getByRole("button", { name: "Verify chain" }));
  expect(await screen.findByText(/Simulated chain hashes and links passed/)).toBeTruthy();
  fireEvent.change(screen.getByLabelText("Payment ID for individual checks"), { target: { value: "pay_04" } });
  fireEvent.click(screen.getByRole("button", { name: "Run payment checks" }));
  expect(await screen.findByText("Invalid signature.")).toBeTruthy();
  expect(screen.getByText("Cart signature").parentElement?.textContent).toContain("Failed");
  expect(screen.getByText("Mandate signature").parentElement?.textContent).toContain("Unavailable");
  screenView.unmount();
  expect(close).toHaveBeenCalled();
});

it("reads the selected mandate details and remaining budget from System 2", async () => {
  const mandate = {
    id: "man_01", user_id: "u1", category: "footwear", item_description: "Shoes",
    status: "ACTIVE", issued_at: ts, expires_at: ts, max_amount_minor: 8000,
    currency: "USD", allowed_merchants: ["merchant-one"],
    key_id: "fixture-key", signature: "mock-signature-mandate-01",
  };
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    const responses: Record<string, unknown> = {
      "/api/mandates": [mandate],
      "/api/mandates/man_01": mandate,
      "/api/mandates/man_01/budget": { currency: "USD", spent_minor: 6500, reserved_minor: 0, remaining_minor: 1500 },
    };
    return Promise.resolve(Response.json(responses[url]));
  }));
  view(<MandatesPage />);
  fireEvent.click(await screen.findByRole("button", { name: "man_01" }));
  expect(await screen.findByText("merchant-one")).toBeTruthy();
  expect(await screen.findByText(/USD\s+15\.00/)).toBeTruthy();
  expect(screen.getByText("Mock engine fixture")).toBeTruthy();
});
