"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Scale } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { Empty, Failed, Pending } from "@/components/dispute-states";
import { Button } from "@/components/ui/button";
import { evidenceHref, loadCounsel, recommendations, type CounselAnalysis } from "@/lib/counsel";
import { api, formatDate, humanize, type Page } from "@/lib/disputes";
import { evidenceSourceTypes, type Issue } from "@/lib/evidence";

function References({ analysis, ids }: { analysis: CounselAnalysis; ids: string[] }) {
  if (!ids.length) return null;
  const records = new Map(analysis.classification.records.map(record => [record.id, record]));
  return (
    <ul className="mt-1 space-y-1 text-xs">
      {ids.map(id => {
        const record = records.get(id);
        return (
          <li key={id} className="wrap-anywhere">
            <Link className="record-link" href={evidenceHref(
              analysis.payment_id, analysis.dispute_record_id, analysis.evidence_pack_version, id,
            )}>{id}</Link>
            {record && <> · {evidenceSourceTypes[record.source_type]} · {humanize(record.verification_status)}</>}
          </li>
        );
      })}
    </ul>
  );
}

function Issues({ analysis, title, issues }: { analysis: CounselAnalysis; title: string; issues: Issue[] }) {
  return (
    <div>
      <h3 className="font-semibold">{title}</h3>
      {issues.length ? <ul className="mt-2 space-y-2">{issues.map((issue, index) => (
        <li key={`${issue.code}-${index}`}>
          {issue.explanation}
          <References analysis={analysis} ids={issue.evidence_ids} />
        </li>
      ))}</ul> : <p className="muted">None recorded.</p>}
    </div>
  );
}

function AnalysisView({ analysis }: { analysis: CounselAnalysis }) {
  const { classification, outcome } = analysis;
  return (
    <>
      <section className="panel">
        <div className="panel-heading flex-wrap">
          <h2>{outcome ? recommendations[outcome.recommendation] : "Analysis failed"}</h2>
          <span className="total-label">Evidence version {analysis.evidence_pack_version}</span>
          <span className="status-pill amber">Human review pending</span>
        </div>
        <dl className="data-grid integration-data">
          <div><dt>Payment</dt><dd>{analysis.payment_id}</dd></div>
          <div><dt>Dispute</dt><dd>{analysis.dispute_id ?? "Unavailable"}</dd></div>
          <div><dt>Complaint category</dt><dd>{analysis.category}</dd></div>
          <div><dt>Model confidence estimate</dt><dd>
            {outcome?.confidence == null ? "Unavailable" : `${Math.round(outcome.confidence * 100)}%`}
          </dd></div>
        </dl>
        <div className="space-y-2 px-5 pb-5 text-sm">
          {outcome && <p>{outcome.executive_summary}</p>}
          {outcome?.conflict && <p role="status" className="notice amber">{outcome.conflict}</p>}
          {analysis.error && <p role="alert">{analysis.error.message}</p>}
          <Link className="record-link" href={evidenceHref(
            analysis.payment_id, analysis.dispute_record_id, analysis.evidence_pack_version,
          )}>Open evidence version {analysis.evidence_pack_version}</Link>
        </div>
      </section>
      {outcome && <>
        <section className="panel">
          <div className="panel-heading"><h2>AI reasoning</h2></div>
          <div className="space-y-4 p-5 text-sm">
            <ul className="space-y-3">{outcome.reasoning.map(finding => (
              <li key={finding.id}>{finding.text}<References analysis={analysis} ids={finding.evidence_ids} /></li>
            ))}</ul>
            <div><h3 className="font-semibold">Supporting evidence</h3>
              {outcome.supporting_evidence_ids.length
                ? <References analysis={analysis} ids={outcome.supporting_evidence_ids} />
                : <p className="muted">No reliable supporting records were cited.</p>}
            </div>
          </div>
        </section>
        <section className="panel">
          <div className="panel-heading"><h2>Merchant response draft</h2></div>
          <div className="space-y-2 p-5 text-sm">
            <p className="muted">Review the draft and its citations before use. Your edits stay in this view.</p>
            <textarea aria-label="Merchant response draft" rows={8} defaultValue={outcome.draft_response}
              className="w-full rounded-lg border border-slate-300 p-3" />
            <References analysis={analysis} ids={outcome.draft_evidence_ids} />
          </div>
        </section>
      </>}
      <section className="panel">
        <div className="panel-heading"><h2>Evidence gaps and verification</h2></div>
        <div className="grid gap-4 p-5 text-sm">
          <Issues analysis={analysis} title="Contradictory evidence" issues={classification.contradictory_evidence} />
          <Issues analysis={analysis} title="Missing evidence" issues={classification.missing_evidence} />
          <div>
            <Issues analysis={analysis} title="Invalid evidence" issues={classification.invalid_evidence} />
            <References analysis={analysis} ids={classification.invalid_evidence_ids} />
          </div>
          {([["Unverified evidence", classification.unverified_evidence_ids],
            ["Unavailable evidence", classification.unavailable_evidence_ids]] as const).map(([title, ids]) => (
            <div key={title}>
              <h3 className="font-semibold">{title}</h3>
              {ids.length ? <References analysis={analysis} ids={ids} /> : <p className="muted">None recorded.</p>}
            </div>
          ))}
          {classification.unavailable_sources.length > 0 && <div><h3 className="font-semibold">Unavailable sources</h3>
            <ul className="space-y-1">{classification.unavailable_sources.map(source => (
              <li key={source.reference}>{source.reference}{source.detail && <> · {source.detail}</>}</li>
            ))}</ul>
          </div>}
          {classification.verification_warnings.length > 0 && <div><h3 className="font-semibold">Verification warnings</h3>
            <ul className="space-y-1">{classification.verification_warnings.map(warning => <li key={warning}>{warning}</li>)}</ul>
          </div>}
        </div>
      </section>
    </>
  );
}

const historySize = 8;

function CounselReference({ payment, dispute }: { payment: string; dispute: string | null }) {
  const queryClient = useQueryClient();
  const [selected, selectAnalysis] = useState<CounselAnalysis | null>(null);
  const [page, setPage] = useState(1);
  const base = `/counsel/${payment}`;
  const scope = new URLSearchParams(dispute ? { dispute_id: dispute } : undefined);
  const query = scope.size ? `?${scope}` : "";
  const key = ["counsel", payment, dispute];
  const latest = useQuery({ queryKey: key, queryFn: ({ signal }) => loadCounsel(base + query, signal) });
  const historyQuery = new URLSearchParams(scope);
  historyQuery.set("page", String(page));
  historyQuery.set("page_size", String(historySize));
  const history = useQuery({
    queryKey: [...key, "history", page],
    queryFn: ({ signal }) => api<Page<CounselAnalysis>>(`${base}/history?${historyQuery}`, { signal }),
  });
  const run = useMutation({
    mutationFn: (force: boolean) => {
      const options = new URLSearchParams(scope);
      if (force) options.set("force", "true");
      return api<CounselAnalysis>(`${base}/analyze${options.size ? `?${options}` : ""}`, { method: "POST" });
    },
    onSuccess: async result => {
      await queryClient.cancelQueries({ queryKey: key, exact: true });
      queryClient.setQueryData(key, result);
      selectAnalysis(null);
      setPage(1);
      void queryClient.invalidateQueries({ queryKey: [...key, "history"] });
    },
    onError: () => {
      void queryClient.invalidateQueries({ queryKey: key });
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
  const shown = selected ?? latest.data;
  return (
    <>
      <div className="actions mb-4">
        <Button disabled={run.isPending || latest.isPending} onClick={() => run.mutate(false)}>
          {run.isPending && <LoaderCircle size={16} className="animate-spin" aria-hidden="true" />}
          {run.isPending ? "Analyzing evidence…" : latest.data?.error ? "Retry analysis" : "Analyze latest evidence"}
        </Button>
        {latest.data?.outcome && <Button variant="outline" disabled={run.isPending} onClick={() => run.mutate(true)}>Run again</Button>}
        <Button asChild variant="outline"><Link href={evidenceHref(payment, dispute)}>Open latest evidence</Link></Button>
      </div>
      {run.isError && run.error.message !== shown?.error?.message && <div className="notice red" role="alert">{run.error.message}</div>}
      {latest.isPending ? <Pending message="Loading Counsel analysis…" />
        : latest.isError && !shown ? <Failed error={latest.error} retry={() => void latest.refetch()} />
        : shown ? <AnalysisView analysis={shown} key={shown.id} />
        : <section className="panel"><Empty title="No Counsel analysis yet" message="Assemble evidence for this payment, then run analysis." /></section>}
      <section className="panel">
        <div className="panel-heading"><h2>Analysis history</h2></div>
        {history.isPending ? <Pending message="Loading analysis history…" />
          : history.isError ? <Failed error={history.error} retry={() => void history.refetch()} />
          : history.data.total === 0 ? <Empty title="No saved attempts" message="Each analysis attempt appears here." />
          : <div className="space-y-3 p-5">
            <ul className="space-y-2">{history.data.items.map(entry => (
              <li key={entry.id}>
                <Button variant="outline" aria-pressed={shown?.id === entry.id} onClick={() => selectAnalysis(entry)}>
                  #{entry.id} · {formatDate(entry.created_at, true)} · v{entry.evidence_pack_version} · {humanize(entry.analysis_status)}
                </Button>
              </li>
            ))}</ul>
            <div className="actions">
              <Button variant="outline" disabled={page === 1} onClick={() => setPage(page - 1)}>Previous</Button>
              <Button variant="outline" disabled={page * historySize >= history.data.total} onClick={() => setPage(page + 1)}>Next</Button>
              <span className="muted">Page {page}</span>
            </div>
          </div>}
      </section>
    </>
  );
}

function CounselSearch() {
  const search = useSearchParams();
  const payment = search.get("payment_id")?.trim() ?? "";
  const dispute = search.get("dispute_id");
  const valid = /^[A-Za-z0-9_-]{1,255}$/.test(payment) && (dispute === null || /^[1-9]\d{0,9}$/.test(dispute));
  return <>
    <form action="/counsel" className="panel flex flex-wrap items-end gap-2 p-4">
      <label className="grid gap-1 text-sm font-semibold">Gawah payment ID
        <input name="payment_id" className="rounded-lg border border-slate-300 px-3 py-2 font-normal"
          key={payment} defaultValue={payment} maxLength={255} placeholder="Enter a payment ID" required />
      </label>
      <Button type="submit" variant="outline">Find payment</Button>
    </form>
    {valid ? <CounselReference key={`${payment}/${dispute}`} payment={payment} dispute={dispute} />
      : payment && <Failed error={new Error("Enter a valid payment or dispute reference.")} />}
  </>;
}

export default function CounselPage() {
  return <>
    <div className="page-heading">
      <div>
        <p className="eyebrow"><Scale size={15} aria-hidden="true" /> CASE REVIEW</p>
        <h1>AI Counsel</h1>
        <p>Review evidence-grounded recommendations and merchant drafts.</p>
      </div>
    </div>
    <Suspense fallback={<Pending message="Loading AI Counsel…" />}><CounselSearch /></Suspense>
  </>;
}
