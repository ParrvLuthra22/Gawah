"use client";

import Link from "next/link";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Empty, Failed, Pending, Status } from "@/components/dispute-states";
import { Button } from "@/components/ui/button";
import { api, formatAmount } from "@/lib/disputes";
import { evidenceHref, recommendations, type CounselAnalysis } from "@/lib/counsel";

type Budget = { currency: string; max_amount_minor: number; spent_minor: number; reserved_minor: number; remaining_minor: number };
type Verification = { ok: boolean; checked: number; reason?: string | null; checks?: { name: string; ok: boolean; detail: string }[] };

// Step 10 and 11. Reads existing dashboard endpoints (budget, ledger verification, Counsel) for the
// payment that was just made. Dispute results here come from Counsel; the demo disputes elsewhere are
// SIMULATED_FIXTURE records and are labelled as such on /disputes.
export function EvidencePanel({ mandateId, paymentId, simulated }: { mandateId: string; paymentId: string | null; simulated: boolean }) {
  const budget = useQuery({ queryKey: ["mandates", mandateId, "budget"], queryFn: ({ signal }) => api<Budget>(`/mandates/${mandateId}/budget`, { signal }) });
  const ledger = useQuery({ queryKey: ["ledger", "verify"], queryFn: ({ signal }) => api<Verification>("/ledger/verify", { signal }) });
  const payment = useQuery({
    queryKey: ["ledger", "payment", paymentId], enabled: !!paymentId,
    queryFn: ({ signal }) => api<Verification>(`/ledger/payments/${paymentId}/verification`, { signal }),
  });
  const counsel = useMutation({ mutationFn: () => api<CounselAnalysis>(`/counsel/${paymentId}/analyze`, { method: "POST" }) });
  return <section className="panel" aria-labelledby="shop-evidence">
    <div className="panel-heading"><h2 id="shop-evidence">Evidence and Counsel</h2></div>
    <div className="notice blue panel-notice"><p>Budget and ledger results below are for the {simulated ? <strong>SIMULATED</strong> : "PayPal sandbox"} transaction{simulated ? " (the engine's fake PayPal, no sandbox call was made)" : ""}.
      Dispute scenarios elsewhere in the console are <strong>SIMULATED</strong> fixtures; a fresh purchase has no dispute.</p></div>
    {budget.isPending ? <Pending message="Loading budget…" /> : budget.isError ? <Failed error={budget.error} retry={() => void budget.refetch()} />
      : <dl className="data-grid">
        <div><dt>Mandate cap</dt><dd>{formatAmount(budget.data.max_amount_minor, budget.data.currency)}</dd></div>
        <div><dt>Spent</dt><dd>{formatAmount(budget.data.spent_minor, budget.data.currency)}</dd></div>
        <div><dt>Reserved</dt><dd>{formatAmount(budget.data.reserved_minor, budget.data.currency)}</dd></div>
        <div><dt>Remaining</dt><dd>{formatAmount(budget.data.remaining_minor, budget.data.currency)}</dd></div>
      </dl>}
    <div className="data-grid">
      <div><dt>Ledger chain</dt><dd>{ledger.isPending ? "Checking…" : ledger.isError ? ledger.error.message
        : <Status value={ledger.data.ok ? "SUCCESS" : "FAILED"} />}{ledger.data && ` ${ledger.data.checked} entries`}</dd></div>
      {paymentId && <div><dt>Payment evidence</dt><dd>{payment.isPending ? "Checking…" : payment.isError ? payment.error.message
        : <Status value={payment.data.ok ? "SUCCESS" : "FAILED"} />}</dd></div>}
    </div>
    {payment.data?.checks?.length ? <ul className="shop-steps">{payment.data.checks.map(c =>
      <li key={c.name} className={`status-pill ${c.ok ? "green" : "red"}`} title={c.detail}>{c.name.replaceAll("_", " ")}</li>)}</ul> : null}
    {paymentId ? <div className="actions" style={{ padding: "16px 20px" }}>
      <Button variant="outline" asChild><Link href={evidenceHref(paymentId)}>Open evidence pack</Link></Button>
      <Button variant="outline" asChild><Link href={`/counsel?payment_id=${encodeURIComponent(paymentId)}`}>Open AI Counsel</Link></Button>
      <Button disabled={counsel.isPending} onClick={() => counsel.mutate()}>{counsel.isPending ? "Analyzing…" : "Run Counsel analysis"}</Button>
    </div> : <Empty title="No payment yet" message="Evidence appears once a payment exists." />}
    {counsel.isError && <Failed error={counsel.error} />}
    {counsel.data && <div className="notice blue panel-notice">
      {counsel.data.outcome ? <><strong>{recommendations[counsel.data.outcome.recommendation]}</strong>
        <p>{counsel.data.outcome.executive_summary}</p></>
        : <p>{counsel.data.error?.message ?? "Counsel returned no recommendation."}</p>}
      <p className="muted">Advisory only; human review required. No dispute exists for a new purchase.</p></div>}
  </section>;
}
