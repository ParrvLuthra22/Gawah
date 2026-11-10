"use client";

import Link from "next/link";
import { Breakdown } from "@/components/dashboard-analytics";
import { Panel, Remote, WorkflowBadge } from "@/components/dashboard-panels";
import { Empty } from "@/components/dispute-states";
import { type ActivityFeed, type ArtifactRef, type CounselStats, type EvidenceStats, useDashboard, workflowHref } from "@/lib/dashboard";
import { formatDate, humanize, sourceLabels } from "@/lib/disputes";

function Reference({ row }: { row: Pick<ArtifactRef, "paypal_dispute_id" | "dispute_id" | "source"> & { payment_id: string | null } }) {
  return <span className="muted">
    {row.dispute_id !== null
      ? <Link className="record-link" href={`/disputes/${row.dispute_id}`}>
        {row.paypal_dispute_id ?? `Case #${row.dispute_id}`}
      </Link> : row.payment_id ?? "No dispute reference"}
    {row.source ? ` · ${sourceLabels[row.source]}` : " · Standalone workflow"}
  </span>;
}

export function EvidenceMonitoring({ result }: { result: ReturnType<typeof useDashboard<EvidenceStats>> }) {
  return <Panel title="Evidence monitoring" id="evidence-monitoring"
    detail={<Link href="/evidence" className="record-link text-sm">Open evidence</Link>}>
    <Remote result={result} label="Evidence monitoring">{data => !data.packs_generated
      ? <Empty title="No evidence packs in this period" message="Saved N2 versions appear here after assembly." />
      : <div className="space-y-4 p-5">
        <dl className="dashboard-facts">
          <div><dt>Saved workflows</dt><dd>{data.latest_packs}</dd></div>
          <div><dt>Packs with gaps</dt><dd>{data.packs_with_gaps}</dd></div>
          <div><dt>Missing records</dt><dd>{data.missing_records}</dd></div>
          <div><dt>Invalid records</dt><dd>{data.invalid_records}</dd></div>
          <div><dt>Contradictions</dt><dd>{data.contradictions}</dd></div>
        </dl>
        <Breakdown title="Verification on the latest pack per workflow" groups={data.verification} />
        <div>
          <h3 className="text-sm font-semibold">Latest assemblies</h3>
          <ul className="dashboard-records">{data.recent.map(row => <li key={row.id}>
            <div className="flex flex-wrap justify-between gap-2">
              <Link className="record-link" href={workflowHref("evidence", row.payment_id, row.dispute_id, row.version)}>
                {row.payment_id} · Version {row.version}
              </Link>
              <time className="muted" dateTime={row.generated_at}>{formatDate(row.generated_at, true)}</time>
            </div>
            <Reference row={row} />
            <p className="muted">{row.missing} missing · {row.invalid} invalid · {row.contradictions} contradictory</p>
          </li>)}</ul>
        </div>
      </div>}</Remote>
  </Panel>;
}

export function CounselMonitoring({ result }: { result: ReturnType<typeof useDashboard<CounselStats>> }) {
  return <Panel title="AI Counsel monitoring" id="counsel-monitoring"
    detail={<Link href="/counsel" className="record-link text-sm">Open AI Counsel</Link>}>
    <Remote result={result} label="AI Counsel monitoring">{data => !data.analyses
      ? <Empty title="No AI analyses in this period" message="Completed and failed N3 runs appear here." />
      : <div className="space-y-4 p-5">
        <dl className="dashboard-facts">
          <div><dt>Analyses recorded</dt><dd>{data.analyses}</dd></div>
          <div><dt>Merchant drafts</dt><dd>{data.drafts}</dd></div>
          <div><dt>Accepted recommendations</dt><dd>{data.accepted ?? "Unavailable"}</dd></div>
          <div><dt>Rejected recommendations</dt><dd>{data.rejected ?? "Unavailable"}</dd></div>
        </dl>
        <p className="muted">N3 records its own pending state. Operator decisions appear in Resolution Center.</p>
        <div className="dashboard-columns">
          <Breakdown title="Recommendations" groups={data.by_recommendation} />
          <Breakdown title="Recorded analysis status" groups={data.by_status} />
        </div>
        <div>
          <h3 className="text-sm font-semibold">Recent Counsel activity</h3>
          <ul className="dashboard-records">{data.recent.map(row => <li key={row.id}>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <Link className="record-link" href={workflowHref("counsel", row.payment_id, row.dispute_id)}>
                {row.payment_id} · Analysis #{row.id}
              </Link>
              <WorkflowBadge status={row.status} />
            </div>
            <Reference row={row} />
            <p className="muted">{formatDate(row.created_at, true)} · Evidence v{row.evidence_version}
              {row.recommendation && ` · ${humanize(row.recommendation)} · ${humanize(row.review_status)} review`}</p>
          </li>)}</ul>
        </div>
      </div>}</Remote>
  </Panel>;
}

export function DashboardActivity({ scope }: { scope: string }) {
  const result = useDashboard<ActivityFeed>("activity", scope);
  return <Panel title="Recent activity" id="activity" detail={<span className="muted">Latest 30 recorded events</span>}>
    <Remote result={result} label="Recent activity">{data => <>
      {!!data.unavailable.length && <p className="notice amber m-4" role="status">
        Activity is unavailable for {data.unavailable.map(humanize).join(", ")}.{" "}
        <button type="button" className="record-link" onClick={() => void result.refetch()}>Retry activity sources</button>
      </p>}
      {data.items.length === 0
        ? <Empty title="No recorded activity in this period" message="Events appear when a source saves an update." />
        : <ol className="timeline">{data.items.map(row => <li key={row.id}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <strong>{humanize(row.type)}</strong>
            <time className="muted" dateTime={row.timestamp}>{formatDate(row.timestamp, true)}</time>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <Reference row={row} />
            <WorkflowBadge status={row.status} />
            {row.payment_id && row.evidence_version !== null && <Link className="record-link"
              href={workflowHref("evidence", row.payment_id, row.dispute_id, row.evidence_version)}>View evidence</Link>}
            {row.payment_id && (row.type.startsWith("AI_") || row.type === "MERCHANT_DRAFT_GENERATED")
              && <Link className="record-link" href={workflowHref("counsel", row.payment_id, row.dispute_id)}>View Counsel</Link>}
          </div>
        </li>)}</ol>}
    </>}</Remote>
  </Panel>;
}
