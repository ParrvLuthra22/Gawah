import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import DashboardPage from "@/app/dashboard/page";
import type { CaseRow, Overview } from "@/lib/dashboard";

const overview: Overview = {
  total: 0, open: 0, resolved: 0, awaiting_merchant: 0, due_soon: 0, overdue: 0, unlinked: 0,
  amounts: [], unknown_amounts: 0, unknown_open_dates: 0, resolution_rate: null,
  average_observed_resolution_hours: null, resolution_samples: 0, daily: [],
  by_status: [], by_reason: [], by_outcome: [], latest_sync: null, as_of: "2026-11-10T12:00:00Z",
};
const empty = {
  overview,
  cases: { items: [], total: 0, page: 1, page_size: 25, as_of: overview.as_of, unavailable: [] },
  evidence: { packs_generated: 0, latest_packs: 0, packs_with_gaps: 0, missing_records: 0,
    invalid_records: 0, contradictions: 0, verification_rate: null, verification: [], recent: [] },
  counsel: { analyses: 0, recommendations: 0, pending_reviews: 0, drafts: 0, accepted: null,
    rejected: null, by_recommendation: [], by_status: [], recent: [] },
  activity: { items: [], unavailable: [] },
  resolution: { total: 0, awaiting_approval: 0, pending_information: 0, submitted: 0,
    confirmed: 0, failed: 0, unknown: 0, by_status: {} },
};

function view() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}><DashboardPage /></QueryClientProvider>);
}

function mockApi(overrides: Partial<Record<keyof typeof empty, (url: URL) => Response | Promise<Response>>> = {}) {
  const fetch = vi.fn(async (path: string) => {
    const url = new URL(path, "http://localhost");
    const key = url.pathname.split("/").at(-1) as keyof typeof empty;
    return overrides[key]?.(url) ?? Response.json(empty[key]);
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("renders loading skeletons before showing a genuinely empty dashboard", async () => {
  let resolve!: (value: Response) => void;
  mockApi({ overview: () => new Promise(finish => { resolve = finish; }) });
  view();
  expect(screen.getByText("Loading Dispute analytics…")).toBeTruthy();
  resolve(Response.json(overview));
  expect(await screen.findByText("No disputes in this period")).toBeTruthy();
  expect(await screen.findByText("No evidence packs in this period")).toBeTruthy();
  expect(await screen.findByText("No AI analyses in this period")).toBeTruthy();
  expect(await screen.findByText("No recorded activity in this period")).toBeTruthy();
  expect(screen.getByText("Total disputes").nextElementSibling?.textContent).toBe("0");
  expect(screen.getByText("Evidence verification success rate").nextElementSibling?.textContent).toBe("Unavailable");
});

it("retries failed evidence independently and retains other dashboard sections", async () => {
  let attempts = 0;
  mockApi({ evidence: () => ++attempts === 1
    ? Response.json({ error: { message: "Evidence storage failed." } }, { status: 503 })
    : Response.json(empty.evidence) });
  view();
  const section = screen.getByRole("region", { name: "Evidence monitoring" });
  expect((await within(section).findByRole("alert")).textContent).toContain("Evidence storage failed.");
  expect(await screen.findByText("No disputes match this view")).toBeTruthy();
  expect(await screen.findByText("No AI analyses in this period")).toBeTruthy();
  expect(screen.getByText("Evidence packs generated").nextElementSibling?.textContent).toBe("Unavailable");
  fireEvent.click(within(section).getByRole("button", { name: "Try again" }));
  expect(await screen.findByText("No evidence packs in this period")).toBeTruthy();
});

it("links scoped workflows and sends date, reason, search, sort and page filters to the backend", async () => {
  const row: CaseRow = {
    id: 7, paypal_dispute_id: "PP-D-SIM", payment_id: "pay_04", source: "SIMULATED_FIXTURE",
    link_status: "UNMATCHED", status: "OPEN", reason: "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED",
    amount_minor: 6500, currency: "USD", deadline: "2026-11-11T12:00:00Z",
    created_at: "2026-11-09T12:00:00Z", updated_at: null, evidence_status: "SAVED_WITH_GAPS",
    evidence_version: 2, counsel_status: "STALE", counsel_version: 1,
  };
  const fetch = mockApi({
    cases: url => Response.json({
      ...empty.cases, items: [row], total: 40, page: Number(url.searchParams.get("page")),
    }),
    overview: () => Response.json({
      ...overview, total: 1, open: 1, due_soon: 1, amounts: [{ currency: "USD", amount_minor: 6500 }],
      by_reason: [{ label: row.reason, count: 1 }],
      daily: [{ day: "2026-11-09", count: 1, amounts: [{ currency: "USD", amount_minor: 6500 }] }],
    }),
  });
  view();
  const link = await screen.findByRole("link", { name: "PP-D-SIM" });
  expect(link.getAttribute("href")).toBe("/disputes/7");
  expect(within(link.closest("tr")!).getByText("Simulated fixture")).toBeTruthy();
  expect(screen.getByText("Due within 72h")).toBeTruthy();
  expect(screen.getByRole("link", { name: "View version 2" }).getAttribute("href")).toBe("/evidence?payment_id=pay_04&dispute_id=7&version=2");
  expect(screen.getByRole("link", { name: "View analysis · v1" }).getAttribute("href")).toBe("/counsel?payment_id=pay_04&dispute_id=7");
  expect(await screen.findByRole("img", { name: /Disputes opened over time/ })).toBeTruthy();

  fireEvent.change(screen.getByLabelText("Period"), { target: { value: "custom" } });
  fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-11-01" } });
  fireEvent.change(screen.getByLabelText("Through"), { target: { value: "2026-11-10" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply dates" }));
  await waitFor(() => expect(fetch.mock.calls.some(([url]) =>
    url.includes("start=2026-11-01&end=2026-11-10"))).toBe(true));

  await screen.findByRole("option", { name: /Merchandise or service not as described/i });
  fireEvent.change(screen.getByLabelText("Dispute reason"), { target: { value: row.reason } });
  fireEvent.change(screen.getByLabelText("Search case or payment ID"), { target: { value: "pay_04" } });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  fireEvent.click(await screen.findByRole("button", { name: "PayPal case" }));
  fireEvent.click(await screen.findByRole("button", { name: "Next case page" }));
  await waitFor(() => expect(fetch.mock.calls.some(([path]) => {
    const query = new URL(path, "http://localhost").searchParams;
    return path.startsWith("/api/dashboard/cases?") && query.get("start") === "2026-11-01"
      && query.get("end") === "2026-11-10" && query.get("reason") === row.reason
      && query.get("search") === "pay_04" && query.get("sort") === "paypal_dispute_id"
      && query.get("page") === "2";
  })).toBe(true));
});
