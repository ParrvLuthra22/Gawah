import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import CounselPage from "@/app/counsel/page";
import type { CounselAnalysis } from "@/lib/counsel";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams("payment_id=pay_04&dispute_id=2"),
}));

const result: CounselAnalysis = {
  id: 10, payment_id: "pay_04", dispute_id: "PP-D-CASE", dispute_record_id: 2,
  category: "Non-receipt", evidence_pack_version: 3, analysis_status: "COMPLETED",
  review_status: "PENDING", human_review_required: true, model: "test-model",
  created_at: "2026-11-10T12:00:00Z", error: null,
  classification: {
    records: [{ id: "dispute:PP-D-CASE", source_type: "SIMULATED_FIXTURE", verification_status: "UNVERIFIED" }],
    contradictory_evidence: [], invalid_evidence: [], missing_evidence: [],
    invalid_evidence_ids: [], unverified_evidence_ids: ["dispute:PP-D-CASE"], unavailable_evidence_ids: [],
    unavailable_sources: [{ reference: "/delivery", detail: "Delivery source unavailable." }],
    verification_warnings: ["Payment reference check is unverified."],
  },
  outcome: {
    recommendation: "REQUEST_MORE_EVIDENCE", confidence: null, conflict: null,
    executive_summary: "Request proof of delivery.",
    reasoning: [{ id: "COMPLAINT", text: "The recorded complaint is non-receipt.", evidence_ids: ["dispute:PP-D-CASE"] }],
    supporting_evidence_ids: [],
    draft_response: "Please provide delivery confirmation. [dispute:PP-D-CASE]",
    draft_evidence_ids: ["dispute:PP-D-CASE"],
  },
};

function view() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><CounselPage /></QueryClientProvider>);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("runs Counsel, links exact evidence, and lets the operator edit the draft", async () => {
  let saved = false;
  const fetchStub = vi.fn<typeof fetch>().mockImplementation(async (path, init) => {
    if (init?.method === "POST") {
      saved = true;
      return Response.json(result);
    }
    if (String(path).includes("/history?")) return Response.json({ items: saved ? [result] : [], total: saved ? 1 : 0 });
    return Response.json({ error: { code: "COUNSEL_ANALYSIS_NOT_FOUND" } }, { status: 404 });
  });
  vi.stubGlobal("fetch", fetchStub);
  view();
  await screen.findByText("No Counsel analysis yet");
  fireEvent.click(screen.getByRole("button", { name: "Analyze latest evidence" }));
  expect(await screen.findByRole("heading", { name: "Request more evidence" })).toBeTruthy();
  expect(screen.getByText("Human review pending")).toBeTruthy();
  const evidence = screen.getAllByRole("link", { name: "dispute:PP-D-CASE" })[0];
  expect(evidence.getAttribute("href")).toBe("/evidence?payment_id=pay_04&dispute_id=2&version=3#dispute%3APP-D-CASE");
  expect(within(evidence.closest("li")!).getByText(/Simulated fixture · Unverified/)).toBeTruthy();
  const editor = screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Merchant response draft" });
  fireEvent.change(editor, { target: { value: "Reviewed draft" } });
  expect(editor.value).toBe("Reviewed draft");
  expect(await screen.findByRole("button", { name: /#10.*v3/ })).toBeTruthy();
  expect(fetchStub.mock.calls.some(([path, init]) =>
    path === "/api/counsel/pay_04/analyze?dispute_id=2" && init?.method === "POST",
  )).toBe(true);
});

it("shows a failed attempt and exposes retry without inventing a recommendation", async () => {
  const failed = {
    ...result, outcome: null, analysis_status: "FAILED",
    error: { code: "COUNSEL_RATE_LIMITED", message: "Groq is rate limited." },
  };
  vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockImplementation(async path => Response.json(
    String(path).includes("/history?") ? { items: [failed], total: 1 } : failed,
  )));
  view();
  expect((await screen.findByRole("alert")).textContent).toContain("Groq is rate limited.");
  expect(screen.getByRole("button", { name: "Retry analysis" })).toBeTruthy();
  expect(screen.queryByRole("textbox", { name: "Merchant response draft" })).toBeNull();
});
