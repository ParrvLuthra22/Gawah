import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { Approval } from "@/components/resolution-review";
import { ResolutionSession, useResolutionSession } from "@/components/resolution-session";
import type { Workflow } from "@/lib/resolution";

const timestamp = "2026-11-09T12:00:00Z";
const check = { status: "VERIFIED", detail: "Checked.", result: null, reference: null } as const;
const workflow: Workflow = {
  id: 1, dispute_id: 2, paypal_dispute_id: "PP-D-ONE", payment_id: "pay_01", reason: null,
  amount_minor: 6500, currency: "USD", recommendation: "CONCEDE_REMEDY", strategy: "REMEDY",
  status: "READY_FOR_APPROVAL", assigned_reviewer: "alice", deadline: null, updated_at: timestamp,
  revision: 2, analysis_id: 4, evidence_version: 3, stale: false, actor: { id: "alice", role: "reviewer" },
  counsel: { executive_summary: "Review case", confidence: .7, reasoning: [],
    supporting_evidence_ids: [], draft_response: "Reviewed reply" },
  evidence: [], verification: { mandate_signature: check, cart_signature: check, ledger: check, engine_ledger: check },
  gaps: [], requests: [], attempts: [], activity: [], approval: null, dispute_status: "OPEN", paypal_status: "OPEN",
  paypal_checked_at: timestamp, submitted_at: null, completed_at: null,
  plan: { option: { action: "accept-claim", amount_minor: 6500, currency: "USD" },
    paypal_status: "OPEN", paypal_stage: "CHARGEBACK", capture_id: "CAPTURE123",
    evidence_ids: [], override_reason: null, payload: { accept_claim_type: "REFUND" } },
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("shows the exact preview and separates approval from submission", () => {
  const change = vi.fn();
  render(<Approval workflow={workflow} pending={false} canWrite change={change} />);
  expect(screen.getByLabelText("Exact submission payload").textContent).toContain('"accept_claim_type": "REFUND"');
  expect(screen.getByText(/refunds USD/).textContent).toContain("65.00");
  expect(screen.queryByRole("button", { name: "Submit approved action" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Approve this package" }));
  expect(change).toHaveBeenCalledExactlyOnceWith("review", { command: "approve" });
});

it("requires the operator to type the case ID after an independent approval", () => {
  const change = vi.fn();
  const approval = { id: 1, revision: 3, actor_id: "alice", event_type: "APPROVED",
    previous_status: "READY_FOR_APPROVAL", new_status: "APPROVED", details: {}, created_at: timestamp } as const;
  const approved = { ...workflow, status: "APPROVED", approval } as const;
  const view = render(<Approval workflow={approved} pending={false} canWrite change={change} />);
  expect(screen.queryByRole("button", { name: "Submit approved action" })).toBeNull();
  view.rerender(<Approval workflow={{ ...approved, actor: { id: "sam", role: "operator" } }}
    pending={false} canWrite change={change} />);
  expect(screen.getByText(/Approved by alice/)).toBeTruthy();
  const submit = screen.getByRole<HTMLButtonElement>("button", { name: "Submit approved action" });
  expect(submit.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText("Type PP-D-ONE to confirm submission"), { target: { value: "PP-D-ONE" } });
  expect(submit.disabled).toBe(false);
  fireEvent.click(submit);
  expect(change).toHaveBeenCalledExactlyOnceWith("submit", { confirmation: "PP-D-ONE" });
});

function SignedIn() {
  const { actor } = useResolutionSession();
  return <p>Workflow access for {actor.id}</p>;
}

it("authenticates with the assigned credential and removes workflow access on sign-out", async () => {
  const fetch = vi.fn().mockResolvedValue(Response.json({ id: "alice", role: "reviewer" }));
  vi.stubGlobal("fetch", fetch);
  render(<QueryClientProvider client={new QueryClient()}>
    <ResolutionSession><SignedIn /></ResolutionSession>
  </QueryClientProvider>);
  expect(screen.queryByText(/Workflow access for/)).toBeNull();
  const credential = "alice." + "x".repeat(40);
  fireEvent.change(screen.getByLabelText("Operator credential"), { target: { value: credential } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  expect(await screen.findByText("Workflow access for alice")).toBeTruthy();
  expect(fetch).toHaveBeenCalledExactlyOnceWith("/api/resolution/actor", {
    headers: { Authorization: `Bearer ${credential}` },
  });
  fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
  expect(screen.queryByText(/Workflow access for/)).toBeNull();
  expect(screen.getByLabelText<HTMLInputElement>("Operator credential").value).toBe("");
});
