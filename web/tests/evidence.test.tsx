import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import EvidencePage from "@/app/evidence/page";
import type { EvidencePack } from "@/lib/evidence";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams("payment_id=pay_04&dispute_id=2"),
}));

const verification = {
  status: "UNAVAILABLE" as const, detail: "No matching signature key.", reference: null, result: null,
};
const pack: EvidencePack = {
  id: 7, version: 2, payment_id: "pay_04", dispute_id: "PP-D-FAKE-1002",
  generated_at: "2026-11-09T14:00:00Z",
  evidence: [{
    id: "signed_cart:cart_04", type: "signed_cart", source_system: "MOCK_ENGINE",
    source_record_id: "cart_04", source_reference: "/v1/evidence/pay_04#signed_cart",
    timestamp: null, verification_status: "UNAVAILABLE",
  }],
  timeline: [{
    id: "event:cart_04", type: "SIGNED_CART_RECORDED", timestamp: null, source_system: "MOCK_ENGINE",
    record_id: "cart_04", description: "The source supplied a signed cart.", verification_status: "UNAVAILABLE",
  }],
  verification_summary: {
    mandate_signature: verification, cart_signature: verification, ledger: verification,
    engine_ledger: verification,
  },
  sources: [{ source_system: "SYSTEM_2", status: "UNAVAILABLE", detail: "Source timed out.", reference: "/v1/ledger/verify" }],
  missing_evidence: [], invalid_evidence: [],
  contradictions: [{
    code: "AUTHORIZED_SIZE_MISMATCH", type: "CONTRADICTION", severity: "WARNING",
    explanation: "The mandate specifies size 9, while the signed cart records size 10.",
    evidence_ids: ["signed_cart:cart_04"], source_references: ["/v1/evidence/pay_04#signed_cart"],
  }],
  completeness: { available: 1, missing: 0, verified: 0, unverified: 1 },
};

function view() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><EvidencePage /></QueryClientProvider>);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("assembles a scoped pack and displays the actual timeline, issues and unavailable sources", async () => {
  let finish!: (response: Response) => void;
  const assembling = new Promise<Response>(resolve => { finish = resolve; });
  const fetchStub = vi.fn<typeof fetch>()
    .mockResolvedValueOnce(Response.json({ error: { code: "EVIDENCE_PACK_NOT_FOUND" } }, { status: 404 }))
    .mockReturnValueOnce(assembling);
  vi.stubGlobal("fetch", fetchStub);
  view();
  expect(screen.getByText("Loading evidence pack…")).toBeTruthy();
  await screen.findByText("No evidence pack yet");
  fireEvent.click(screen.getByRole("button", { name: "Assemble evidence" }));
  const pending = await screen.findByRole<HTMLButtonElement>("button", { name: "Assembling evidence…" });
  expect(pending.disabled).toBe(true);
  finish(Response.json(pack));
  expect(await screen.findByText("Evidence pack version 2 is ready to review.")).toBeTruthy();
  expect(fetchStub.mock.calls[1]).toEqual(["/api/evidence/pay_04/assemble?dispute_id=2", { method: "POST" }]);
  expect(screen.getByText("PP-D-FAKE-1002")).toBeTruthy();
  expect(screen.getByText("The source supplied a signed cart.")).toBeTruthy();
  expect(screen.getByText("The mandate specifies size 9, while the signed cart records size 10.")).toBeTruthy();
  expect(screen.getByText("Source timed out.")).toBeTruthy();
});

it("keeps the saved pack visible when a later assembly fails", async () => {
  vi.stubGlobal("fetch", vi.fn<typeof fetch>()
    .mockResolvedValueOnce(Response.json(pack))
    .mockResolvedValueOnce(Response.json({ error: { message: "Evidence collection failed." } }, { status: 502 })));
  view();
  await screen.findByText("Version 2");
  fireEvent.click(screen.getByRole("button", { name: "Assemble evidence" }));
  expect((await screen.findByRole("alert")).textContent).toContain("Evidence collection failed.");
  expect(screen.getByText("Version 2")).toBeTruthy();
  expect(screen.queryByRole("status")).toBeNull();
});

it("shows provenance and named verification separately", async () => {
  const response = {
    ...pack,
    evidence: [{
      ...pack.evidence[0], source_type: "MOCK_RESPONSE",
      verification_status: "UNVERIFIED",
    }, {
      ...pack.evidence[0], id: "dispute:2", type: "dispute",
      source_system: "SYSTEM_3", source_type: "SIMULATED_FIXTURE",
      source_record_id: "2", verification_status: "UNVERIFIED",
    }],
    verification_summary: {
      ...pack.verification_summary,
      engine_ledger: { ...verification, result: { reason: { unexpected: true } } },
      payment_reference_match: {
        ...verification, status: "INVALID", detail: "Payment references disagree.",
      },
      bundle_result: { ok: false, checked: 5 },
    },
  };
  vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValueOnce(Response.json(response)));
  view();
  const table = within(await screen.findByRole("table"));
  expect(table.getByText(/Mock response/)).toBeTruthy();
  expect(table.getByText(/Simulated fixture/)).toBeTruthy();
  expect(table.getAllByText("Unverified")).toHaveLength(2);
  expect(screen.getByText("Payment reference match")).toBeTruthy();
  expect(screen.getByText("Payment references disagree.")).toBeTruthy();
});
