"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileSearch, LoaderCircle } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect } from "react";
import { Empty, Failed, Pending } from "@/components/dispute-states";
import { Button } from "@/components/ui/button";
import { api, formatDate, humanize } from "@/lib/disputes";
import { evidenceSources, evidenceSourceTypes, loadEvidence, type EvidencePack, type Issue } from "@/lib/evidence";

function Badge({ value }: { value: string }) {
  const tone = value === "VERIFIED" || value === "AVAILABLE" ? "green"
    : value === "INVALID" || value === "HIGH" ? "red" : "amber";
  return <span className={`status-pill ${tone}`}>{humanize(value)}</span>;
}

function Issues({ label, issues }: { label: string; issues: Issue[] }) {
  return (
    <div>
      <h3 className="font-semibold">{label} <span className="total-label">{issues.length}</span></h3>
      {issues.length ? <ul className="mt-2 divide-y divide-slate-100">{issues.map((issue, index) => (
        <li className="py-3" key={`${issue.code}-${index}`}>
          <div className="flex flex-wrap items-center gap-2">
            <Badge value={issue.severity} /><strong className="text-sm">{humanize(issue.code)}</strong>
          </div>
          <p className="mt-2 text-sm">{issue.explanation}</p>
          {issue.evidence_ids.length > 0 && <p className="mt-1 text-xs text-slate-600 wrap-anywhere">
            {issue.evidence_ids.join(" · ")}
          </p>}
          {issue.source_references.map(reference => <code className="block text-xs wrap-anywhere" key={reference}>{reference}</code>)}
        </li>
      ))}</ul> : <p className="muted mt-2">None recorded.</p>}
    </div>
  );
}

const verificationLabels = {
  mandate_signature: "Mandate signature",
  cart_signature: "Cart signature",
  payment_reference_match: "Payment reference match",
  ledger: "Payment ledger coverage",
  engine_ledger: "Engine-wide ledger check",
} as const;

function Pack({ pack }: { pack: EvidencePack }) {
  const { available, missing, verified, unverified } = pack.completeness;
  const counselRef = new URLSearchParams({ payment_id: pack.payment_id });
  if (pack.dispute_record_id) counselRef.set("dispute_id", String(pack.dispute_record_id));
  useEffect(() => {
    const hash = window.location.hash.slice(1);
    const match = pack.evidence.find(item => encodeURIComponent(item.id) === hash);
    if (match) document.getElementById(match.id)?.scrollIntoView?.({ block: "center" });
  }, [pack]);
  return (
    <>
      <section className="panel" aria-labelledby="overview">
        <div className="panel-heading flex-wrap">
          <h2 id="overview">Evidence overview</h2>
          <span className="total-label">Version {pack.version}</span>
          <Link className="record-link ml-auto text-sm" href={`/counsel?${counselRef}`}>Open AI Counsel</Link>
        </div>
        <dl className="data-grid integration-data">
          <div><dt>Payment ID</dt><dd>{pack.payment_id}</dd></div>
          <div><dt>Dispute ID</dt><dd>{pack.dispute_id ?? "Unavailable"}</dd></div>
          <div><dt>Generated</dt><dd>{formatDate(pack.generated_at, true)}</dd></div>
          <div><dt>Collected</dt><dd>{available}</dd></div>
          <div><dt>Verified</dt><dd>{verified}</dd></div>
          <div><dt>Unverified</dt><dd>{unverified}</dd></div>
          <div><dt>Missing</dt><dd>{missing}</dd></div>
          <div><dt>Contradictions</dt><dd>{pack.contradictions.length}</dd></div>
        </dl>
        <div className="px-5 pb-5 text-sm">
          <label className="font-semibold" htmlFor="coverage">Collection coverage</label>
          <progress id="coverage" value={available} max={Math.max(1, available + missing)}
            className="mt-2 block h-2 w-full accent-blue-700" />
          <p className="muted mt-2">{available} collected items and {missing} recorded gaps.</p>
        </div>
      </section>
      <section className="panel">
        <div className="panel-heading"><h2>Collected evidence</h2></div>
        <div className="table-scroll">
          <table className="history-table">
            <thead><tr><th scope="col">Evidence</th><th scope="col">Source</th><th scope="col">Timestamp</th>
              <th scope="col">Verification</th><th scope="col">Record ID</th></tr></thead>
            <tbody>{pack.evidence.map(item => (
              <tr key={item.id} id={item.id} className="target:bg-blue-50">
                <td>{humanize(item.type)}</td>
                <td>
                  {evidenceSources[item.source_system]} · {evidenceSourceTypes[item.source_type ?? "UNKNOWN"]}
                  <code className="block text-xs">{item.source_reference}</code>
                </td>
                <td>{item.timestamp ? formatDate(item.timestamp, true) : "Undated"}</td>
                <td>
                  <Badge value={item.verification_status} />
                  {item.verification && <p className="muted mt-1">{item.verification.detail}</p>}
                </td>
                <td>{item.source_record_id}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      </section>
      <section className="panel">
        <div className="panel-heading"><h2>Verification</h2></div>
        <ul className="divide-y divide-slate-100 px-5">
          {(Object.keys(verificationLabels) as (keyof typeof verificationLabels)[]).map(key => {
            const check = pack.verification_summary[key];
            return check && <li className="py-3 text-sm" key={key}>
              <div className="flex flex-wrap items-center gap-2">
                <strong>{verificationLabels[key]}</strong>
                <Badge value={check.status} />
              </div>
              <p className="mt-1">{check.detail}</p>
              {typeof check.result?.reason === "string" && check.result.reason && <p>{check.result.reason}</p>}
              {check.reference && <code className="text-xs wrap-anywhere">{check.reference}</code>}
            </li>;
          })}
        </ul>
      </section>
      <section className="panel">
        <div className="panel-heading"><h2>Transaction timeline</h2></div>
        {pack.timeline.length ? (
          <ol className="timeline">{pack.timeline.map(event => (
            <li key={event.id}>
              <div className="timeline-change"><strong>{event.description}</strong><Badge value={event.verification_status} /></div>
              <p>{event.timestamp ? formatDate(event.timestamp, true) : "Undated"} · {evidenceSources[event.source_system]} · {evidenceSourceTypes[event.source_type ?? "UNKNOWN"]} · {event.record_id}</p>
            </li>
          ))}</ol>
        ) : <Empty title="No timeline events" message="The collected sources supplied no transaction events." />}
      </section>
      <section className="panel">
        <div className="panel-heading"><h2>Evidence issues</h2></div>
        <div className="grid gap-5 p-5">
          <Issues label="Contradictions" issues={pack.contradictions} />
          <Issues label="Invalid evidence" issues={pack.invalid_evidence} />
          <Issues label="Missing evidence" issues={pack.missing_evidence} />
        </div>
      </section>
      <section className="panel">
        <div className="panel-heading"><h2>Source checks</h2></div>
        <ul className="divide-y divide-slate-100 px-5">
          {pack.sources.map(source => <li className="py-3 text-sm" key={source.reference}>
            <div className="flex flex-wrap items-center gap-2"><strong>{evidenceSources[source.source_system]}</strong>
              <Badge value={source.status} /></div>
            <code className="text-xs wrap-anywhere">{source.reference}</code>
            {source.detail && <p>{source.detail}</p>}
          </li>)}
        </ul>
      </section>
    </>
  );
}

function EvidenceReference({ paymentId, disputeId, version }: {
  paymentId: string; disputeId: string | null; version: string | null;
}) {
  const queryClient = useQueryClient();
  const scope = disputeId ? `?dispute_id=${disputeId}` : "";
  const savedScope = version ? `${scope || "?"}${scope ? "&" : ""}version=${version}` : scope;
  const base = `/evidence/${paymentId}`;
  const queryKey = ["evidence", paymentId, disputeId, version];
  const evidence = useQuery({ queryKey, queryFn: ({ signal }) => loadEvidence(base + savedScope, signal) });
  const assemble = useMutation({
    mutationFn: () => api<EvidencePack>(`${base}/assemble${scope}`, { method: "POST" }),
    onSuccess: pack => queryClient.setQueryData(queryKey, pack),
  });
  return (
    <>
      <div className="actions mb-4">
        {version ? <Button asChild variant="outline">
          <Link href={`/evidence?payment_id=${paymentId}${disputeId ? `&dispute_id=${disputeId}` : ""}`}>View latest evidence</Link>
        </Button> : <Button disabled={assemble.isPending || evidence.isPending} onClick={() => assemble.mutate()}>
          {assemble.isPending && <LoaderCircle size={16} className="animate-spin" aria-hidden="true" />}
          {assemble.isPending ? "Assembling evidence…" : "Assemble evidence"}
        </Button>}
        {disputeId && <Button asChild variant="outline"><Link href={`/disputes/${disputeId}`}>Back to dispute</Link></Button>}
      </div>
      {assemble.isError && <div className="notice red" role="alert">{assemble.error.message}</div>}
      {assemble.isSuccess && <div className="notice blue" role="status">Evidence pack version {assemble.data.version} is ready to review.</div>}
      {evidence.isPending ? <Pending message="Loading evidence pack…" />
        : evidence.isError ? <Failed error={evidence.error} retry={() => void evidence.refetch()} />
        : evidence.data ? <Pack pack={evidence.data} />
        : <section className="panel"><Empty title="No evidence pack yet" message="Assemble evidence for this payment to start a pack." /></section>}
    </>
  );
}

function EvidenceSearch() {
  const search = useSearchParams();
  const payment = search.get("payment_id")?.trim() ?? "";
  const dispute = search.get("dispute_id");
  const version = search.get("version");
  const valid = /^[A-Za-z0-9_-]{1,255}$/.test(payment)
    && [dispute, version].every(value => value === null || /^[1-9]\d{0,9}$/.test(value));
  return (
    <>
      <form action="/evidence" className="panel flex flex-wrap items-end gap-2 p-4">
        <label className="grid gap-1 text-sm font-semibold">
          Gawah payment ID
          <input name="payment_id" className="rounded-lg border border-slate-300 px-3 py-2 font-normal"
            defaultValue={payment} key={payment} maxLength={255} placeholder="Enter a payment ID" required />
        </label>
        <Button type="submit" variant="outline">Find payment</Button>
      </form>
      {valid ? <EvidenceReference key={`${payment}/${dispute}/${version}`} paymentId={payment} disputeId={dispute} version={version} />
        : payment && <Failed error={new Error("Enter a valid payment or dispute reference.")} />}
    </>
  );
}

export default function EvidencePage() {
  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow"><FileSearch size={15} aria-hidden="true" /> CASE MATERIALS</p>
          <h1>Evidence</h1>
          <p>Assemble source records, review verification results, and trace a payment.</p>
        </div>
      </div>
      <Suspense fallback={<Pending message="Loading evidence…" />}><EvidenceSearch /></Suspense>
    </>
  );
}
