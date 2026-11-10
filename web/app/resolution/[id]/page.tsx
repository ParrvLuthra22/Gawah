"use client";

import { Suspense } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { Panel, Remote, Skeleton, WorkflowBadge } from "@/components/dashboard-panels";
import { Approval, DraftReview, InformationReview, WorkflowHistory, type Change } from "@/components/resolution-review";
import { useResolutionSession } from "@/components/resolution-session";
import { Button } from "@/components/ui/button";
import { percent, workflowHref } from "@/lib/dashboard";
import { formatAmount, formatDate, humanize } from "@/lib/disputes";
import { editable, type Option, type Workflow } from "@/lib/resolution";

const checks = ["mandate_signature", "cart_signature", "ledger", "payment_reference_match", "engine_ledger"] as const;

function ReviewContext({ data }: { data: Workflow }) {
  return <div className="dashboard-columns">
    <Panel title="Dispute and provider status">
      <dl className="data-grid">
        <div><dt>Payment</dt><dd>{data.payment_id}</dd></div>
        <div><dt>Disputed amount</dt><dd>{formatAmount(data.amount_minor, data.currency)}</dd></div>
        <div><dt>Reason</dt><dd>{humanize(data.reason)}</dd></div>
        <div><dt>Response deadline</dt><dd>{formatDate(data.deadline, true)}</dd></div>
        <div><dt>Last N1 status</dt><dd><WorkflowBadge status={data.dispute_status} /></dd></div>
        <div><dt>Last N5 PayPal check</dt><dd><WorkflowBadge status={data.paypal_status} />
          <span className="muted block">{formatDate(data.paypal_checked_at, true)}</span></dd></div>
        <div><dt>Submitted</dt><dd>{formatDate(data.submitted_at, true)}</dd></div>
        <div><dt>Completed</dt><dd>{formatDate(data.completed_at, true)}</dd></div>
      </dl>
    </Panel>
    <Panel title="Pinned AI recommendation"
      detail={<Link className="record-link text-sm" href={workflowHref("counsel", data.payment_id, data.dispute_id)}>Open AI Counsel</Link>}>
      <div className="space-y-3 p-5">
        <div className="actions"><WorkflowBadge status={data.recommendation} />
          <span className="muted">Analysis #{data.analysis_id} · Confidence {percent(data.counsel.confidence)}</span></div>
        <p>{data.counsel.executive_summary}</p>
        <ul className="list-disc space-y-2 pl-5 text-sm">{data.counsel.reasoning.map(finding => <li key={finding.id}>
          {finding.text}
          {!!finding.evidence_ids.length && <p className="muted">Citations {finding.evidence_ids.join(", ")}</p>}
        </li>)}</ul>
        <details>
          <summary className="record-link cursor-pointer text-sm">View original merchant draft</summary>
          <p className="mt-2 whitespace-pre-wrap text-sm">{data.counsel.draft_response}</p>
        </details>
      </div>
    </Panel>
    <Panel title="Evidence and information checklist"
      detail={<Link className="record-link text-sm" href={workflowHref("evidence", data.payment_id, data.dispute_id, data.evidence_version)}>
        Open evidence version {data.evidence_version}
      </Link>}>
      <div className="space-y-4 p-5">
        <p className="muted">{data.evidence.length} pinned records · {data.gaps.length} evidence issues</p>
        {!!data.counsel.supporting_evidence_ids.length && <p className="muted">
          Counsel citations {data.counsel.supporting_evidence_ids.join(", ")}
        </p>}
        <ul className="space-y-2 text-sm">{data.gaps.map((issue, index) => <li key={`${issue.code}-${index}`}>
          <WorkflowBadge status={issue.type} /> <strong>{humanize(issue.code)}</strong>
          <p className="muted">{issue.explanation}</p>
        </li>)}</ul>
        {!data.gaps.length && <p className="muted">The pinned pack has no recorded gaps.</p>}
      </div>
    </Panel>
    <Panel title="Verification results">
      <ul className="space-y-2 p-5 text-sm">{checks.map(key => {
        const result = data.verification[key];
        return <li key={key}>
          <strong>{humanize(key)}</strong> · <WorkflowBadge status={result?.status ?? null} />
          {result && <p className="muted">{result.detail}</p>}
        </li>;
      })}</ul>
    </Panel>
  </div>;
}

function WorkflowDetail() {
  const { id } = useParams<{ id: string }>();
  const { actor, cacheKey, call } = useResolutionSession();
  const client = useQueryClient();
  const valid = /^[1-9]\d{0,9}$/.test(id);
  const key = ["resolution", cacheKey, "workflow", id];
  const path = `/workflows/${id}`;
  const workflow = useQuery({
    queryKey: key, enabled: valid, queryFn: ({ signal }) => call<Workflow>(path, { signal }),
  });
  const local = process.env.NODE_ENV === "development";
  const canReview = local && actor.role !== "viewer";
  const canEdit = canReview && !!workflow.data && editable.includes(workflow.data.status)
    && (actor.role === "operator" || !workflow.data.assigned_reviewer || workflow.data.assigned_reviewer === actor.id);
  const options = useQuery({
    queryKey: [...key, "options", workflow.data?.revision], enabled: canEdit,
    queryFn: ({ signal }) => call<{ options: Option[]; message: string | null }>(`${path}/options`, { signal }),
  });
  const mutation = useMutation({
    mutationFn: ({ operation, fields }: { operation: string; fields: Record<string, unknown> }) =>
      call<Workflow>(`${path}/${operation}`, { method: "POST", body: JSON.stringify(fields) }),
    onSuccess: result => client.setQueryData(key, result),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: ["resolution", cacheKey, "overview"] });
      void client.invalidateQueries({ queryKey: ["resolution", cacheKey, "queue"] });
      void client.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
  const change: Change = (operation, fields) => {
    if (workflow.data && !mutation.isPending) {
      mutation.mutate({ operation, fields: { revision: workflow.data.revision, ...fields } });
    }
  };
  if (!valid) return <p className="notice red" role="alert">Workflow not found.</p>;
  return <Remote result={workflow} label="Resolution workflow">{data => <>
    <div className="page-heading">
      <div><p className="eyebrow">RESOLUTION WORKFLOW #{data.id}</p>
        <h1>{data.paypal_dispute_id}</h1><p>{humanize(data.strategy)} · {data.assigned_reviewer ?? "Reviewer unassigned"}</p></div>
      <div className="actions">
        <WorkflowBadge status={data.status} />
        <Button asChild variant="outline"><Link href={`/disputes/${data.dispute_id}`}>Open dispute</Link></Button>
        <Button asChild variant="outline"><Link href="/resolution">Resolution queue</Link></Button>
      </div>
    </div>
    {!local && <p className="notice blue">Workflow changes are available in the local development console.</p>}
    {data.stale && <div className="notice amber" role="status">
      This workflow has newer inputs. Refresh from the latest evidence and successful Counsel analysis before approval or submission.
      {canEdit && <Button variant="outline" className="mt-2 block" disabled={mutation.isPending}
        onClick={() => change("review", { command: "refresh" })}>Refresh workflow inputs</Button>}
    </div>}
    {mutation.isError && <div className="notice red" role="alert">{mutation.error.message}{" "}
      <button type="button" className="record-link" onClick={() => void workflow.refetch()}>Refresh workflow</button>
    </div>}
    {data.status === "SUBMITTED" && <p className="notice blue">PayPal acknowledged the action. Reconcile it to confirm the outcome.</p>}
    {data.status === "UNKNOWN" && <p className="notice amber">The outcome is uncertain. Reconcile using the provider record before taking further action.</p>}
    {data.status === "FAILED" && <p className="notice red">The provider rejected the action. Review the submission history before preparing a new package.</p>}
    {canReview && ["UNKNOWN", "SUBMITTED", "SUBMITTING"].includes(data.status) &&
      <Button className="mb-4" variant="outline" disabled={mutation.isPending}
        onClick={() => change("reconcile", {})}>Reconcile with PayPal</Button>}
    <ReviewContext data={data} />
    <Approval key={`approval-${data.revision}`} workflow={data} change={change} canWrite={canEdit} pending={mutation.isPending} />
    {canEdit && <DraftReview key={`draft-${data.revision}`} workflow={data} change={change}
      pending={mutation.isPending} options={options} />}
    <InformationReview key={`info-${data.revision}`} workflow={data} change={change}
      pending={mutation.isPending} canWrite={canEdit} />
    <WorkflowHistory workflow={data} />
  </>}</Remote>;
}

export default function ResolutionDetailPage() {
  return <Suspense fallback={<Skeleton label="Loading resolution workflow…" />}><WorkflowDetail /></Suspense>;
}
