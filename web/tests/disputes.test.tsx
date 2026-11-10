import type { ColDef, ICellRendererParams } from "ag-grid-community";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import DetailPage from "@/app/disputes/[id]/page";
import DisputesPage from "@/app/disputes/page";
import PayPalPage from "@/app/integrations/paypal/page";
import type { Dispute, DisputeDetail, SyncResult } from "@/lib/disputes";

vi.mock("next/navigation", () => ({ useParams: () => ({ id: "7" }) }));
vi.mock("ag-grid-react", () => ({
  AgGridReact: ({ rowData, columnDefs }: { rowData: Dispute[]; columnDefs: ColDef<Dispute>[] }) => (
    <div>{rowData.map(data => {
      const cell = columnDefs[0].cellRenderer;
      return <div key={data.id}>{cell({ data } as ICellRendererParams<Dispute>)}</div>;
    })}</div>
  ),
}));

const dispute: Dispute = {
  id: 7, paypal_dispute_id: "PP-D-TEST", transaction_id: "CAPTURE123",
  payment_id: "pay_01", link_status: "LINKED", reason: "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
  status: "OPEN", amount_minor: 6500, currency: "USD", created_at: "2026-11-09T12:00:00Z",
  updated_at: null, deadline: null, source: "PAYPAL_API", last_synced_at: "2026-11-09T14:00:00Z",
};
const detail: DisputeDetail = {
  ...dispute,
  buyer: { name: "Sandbox Buyer", email: null, merchant_id: null },
  merchant: null,
  status_history: [{
    previous_status: null, new_status: "OPEN", changed_at: dispute.last_synced_at, source: "PAYPAL_API",
  }],
};
const result: SyncResult = {
  id: 1, started_at: dispute.last_synced_at, finished_at: dispute.last_synced_at, result: "SUCCESS",
  retrieved: 1, created: 1, updated: 0, unmatched: 0, errors: [],
};
const list = (items: Dispute[]) => ({ items, total: items.length, page: 1, page_size: 25 });
const fetchStub = vi.fn<typeof fetch>();

function view(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{children}</QueryClientProvider>);
}

beforeEach(() => {
  fetchStub.mockReset();
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Disputes console", () => {
  it("shows loading, then renders and filters cases from the backend", async () => {
    fetchStub.mockResolvedValueOnce(Response.json(list([dispute])))
      .mockResolvedValueOnce(Response.json(list([])));
    view(<DisputesPage />);
    expect(screen.getByText("Loading disputes…")).toBeTruthy();
    const link = await screen.findByRole("link", { name: "PP-D-TEST" });
    expect(link.getAttribute("href")).toBe("/disputes/7");
    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "SIMULATED_FIXTURE" } });
    expect(await screen.findByText("No matching disputes")).toBeTruthy();
    expect(String(fetchStub.mock.calls[1][0])).toContain("source=SIMULATED_FIXTURE");
  });

  it("shows backend failures and an empty result after retry", async () => {
    fetchStub.mockResolvedValueOnce(Response.json({ error: { message: "Backend unavailable." } }, { status: 503 }))
      .mockResolvedValueOnce(Response.json(list([])));
    view(<DisputesPage />);
    expect((await screen.findByRole("alert")).textContent).toContain("Backend unavailable.");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("No disputes to display")).toBeTruthy();
  });

  it("refreshes the list after a successful sync", async () => {
    let finishSync!: (response: Response) => void;
    const pendingSync = new Promise<Response>(resolve => { finishSync = resolve; });
    fetchStub.mockResolvedValueOnce(Response.json(list([])))
      .mockReturnValueOnce(pendingSync)
      .mockResolvedValueOnce(Response.json(list([dispute])));
    view(<DisputesPage />);
    await screen.findByText("No disputes to display");
    fireEvent.click(screen.getByRole("button", { name: "Sync disputes" }));
    const button = await screen.findByRole<HTMLButtonElement>("button", { name: "Syncing disputes…" });
    expect(button.disabled).toBe(true);
    finishSync(Response.json(result));
    expect(await screen.findByText("Sync complete.")).toBeTruthy();
    expect(await screen.findByRole("link", { name: "PP-D-TEST" })).toBeTruthy();
    expect(fetchStub.mock.calls[1]).toEqual(["/api/disputes/sync", { method: "POST" }]);
  });

  it("reports failed and partial runs without showing a success notice", async () => {
    fetchStub.mockImplementation(async (path, init) => {
      if (init?.method !== "POST") return Response.json(list([]));
      if (String(path) === "/api/disputes/sync" && fetchStub.mock.calls.filter(
        ([, options]) => options?.method === "POST",
      ).length === 1) {
        return Response.json({ error: { message: "PayPal timed out." } }, { status: 504 });
      }
      return Response.json({ ...result, result: "PARTIAL", errors: ["System 2 unavailable."] });
    });
    view(<DisputesPage />);
    await screen.findByText("No disputes to display");
    fireEvent.click(screen.getByRole("button", { name: "Sync disputes" }));
    expect((await screen.findByRole("alert")).textContent).toContain("PayPal timed out.");
    fireEvent.click(screen.getByRole("button", { name: "Sync disputes" }));
    expect(await screen.findByText("Sync partially completed.")).toBeTruthy();
    expect(screen.queryByText("Sync complete.")).toBeNull();
  });

  it("renders details, nullable fields, and status history", async () => {
    fetchStub.mockResolvedValueOnce(Response.json(detail));
    view(<DetailPage />);
    expect(screen.getByText("Loading dispute…")).toBeTruthy();
    expect(await screen.findByRole("heading", { name: "PP-D-TEST" })).toBeTruthy();
    expect(screen.getByText("pay_01")).toBeTruthy();
    expect(screen.getByText("Sandbox Buyer")).toBeTruthy();
    expect(screen.getByText("PayPal did not provide these details.")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Status history" })).toBeTruthy();
  });

  it("shows a missing dispute error", async () => {
    fetchStub.mockResolvedValueOnce(Response.json({ error: { message: "Dispute not found." } }, { status: 404 }));
    view(<DetailPage />);
    expect((await screen.findByRole("alert")).textContent).toContain("Dispute not found.");
  });

  it("displays Sandbox connectivity separately from previous sync results", async () => {
    fetchStub.mockImplementation(async path => String(path).includes("/paypal/status")
      ? Response.json({
        environment: "sandbox", credentials_configured: true, connection: "DISCONNECTED",
        last_sync: result, total_disputes: 1, error: "PayPal returned HTTP 403.",
      }) : Response.json({ ...list([]), items: [result], total: 1 }));
    view(<PayPalPage />);
    expect(await screen.findByText("Disconnected")).toBeTruthy();
    expect(await screen.findByText("PayPal returned HTTP 403.")).toBeTruthy();
    await waitFor(() => expect(screen.getByRole("table")).toBeTruthy());
  });
});
