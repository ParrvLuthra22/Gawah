"use client";

import { useState, type ReactNode } from "react";
import { useIsFetching, useQueryClient } from "@tanstack/react-query";
import { LayoutDashboard, RotateCw } from "lucide-react";
import Link from "next/link";
import { DashboardAnalytics } from "@/components/dashboard-analytics";
import { DashboardCases } from "@/components/dashboard-cases";
import { CounselMonitoring, DashboardActivity, EvidenceMonitoring } from "@/components/dashboard-monitoring";
import { Panel, Remote, WorkflowBadge } from "@/components/dashboard-panels";
import { ResolutionStats } from "@/components/resolution-stats";
import { SyncButton, SyncFeedback, useSync } from "@/components/dispute-states";
import { Button } from "@/components/ui/button";
import { percent, type CounselStats, type EvidenceStats, type Overview, useDashboard } from "@/lib/dashboard";
import { formatAmount, formatDate, sourceLabels } from "@/lib/disputes";
import type { Stats } from "@/lib/resolution";

type Range = { start: string; end: string };

function lastDays(days: number): Range {
  const end = new Date();
  const start = new Date(end);
  start.setUTCDate(end.getUTCDate() - days + 1);
  return { start: start.toISOString().slice(0, 10), end: end.toISOString().slice(0, 10) };
}

function Metric({ label, value, pending, hint }: {
  label: string; value?: ReactNode; pending?: boolean; hint?: string;
}) {
  return <div className="dashboard-metric">
    <dt>{label}</dt>
    <dd>{pending ? <span className="block h-6 w-16 animate-pulse rounded bg-blue-100 motion-reduce:animate-none">
      <span className="sr-only">Loading</span>
    </span> : typeof value === "number" ? value.toLocaleString("en") : value ?? "Unavailable"}</dd>
    {hint && <dd className="dashboard-metric-hint muted">{hint}</dd>}
  </div>;
}

function SyncState({ data }: { data: Overview }) {
  const state = data.latest_sync;
  const stale = state && Date.parse(data.as_of) - Date.parse(state.finished_at) > 24 * 3_600_000;
  return <div className="flex flex-wrap items-center gap-2 text-sm">
    <strong>PayPal sync</strong>
    {state ? <>
      <WorkflowBadge status={state.result} />
      <time className="muted" dateTime={state.finished_at}>{formatDate(state.finished_at, true)}</time>
      {stale && <span className="status-pill amber">Older than 24h</span>}
    </> : <span className="muted">No sync recorded</span>}
  </div>;
}

export default function DashboardPage() {
  const [preset, setPreset] = useState("30");
  const [range, setRange] = useState<Range | number | null>(30);
  const [draft, setDraft] = useState<Range>({ start: "", end: "" });
  const [source, setSource] = useState("");
  const scope = new URLSearchParams();
  if (typeof range === "number") scope.set("days", String(range));
  else if (range) {
    scope.set("start", range.start);
    scope.set("end", range.end);
  }
  if (source) scope.set("source", source);
  const query = scope.toString();
  const overview = useDashboard<Overview>("overview", query);
  const evidence = useDashboard<EvidenceStats>("evidence", query);
  const counsel = useDashboard<CounselStats>("counsel", query);
  const resolution = useDashboard<Stats>("resolution", query);
  const client = useQueryClient();
  const refreshing = useIsFetching({ queryKey: ["dashboard"] }) > 0;
  const sync = useSync();
  const counts = overview.data;
  const evidenceStats = evidence.data;
  const counselStats = counsel.data;

  return <>
    <div className="page-heading">
      <div>
        <p className="eyebrow"><LayoutDashboard size={15} aria-hidden="true" /> SYSTEM 3 · OPERATIONS</p>
        <h1>Central Command Dashboard</h1>
        <p>Monitor dispute deadlines, evidence gaps, and decisions awaiting review.</p>
      </div>
      <div className="actions">
        <Button variant="outline" disabled={refreshing}
          onClick={() => void client.invalidateQueries({ queryKey: ["dashboard"] })}>
          <RotateCw size={15} aria-hidden="true" /> Refresh
        </Button>
        <SyncButton sync={sync} />
      </div>
    </div>
    <SyncFeedback sync={sync} />
    <div className="panel">
      <div className="filters" aria-label="Dashboard scope">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <label htmlFor="dashboard-period">Period</label>
          <select id="dashboard-period" value={preset} onChange={event => {
            const value = event.target.value;
            setPreset(value);
            if (value === "custom") setDraft(typeof range === "number" ? lastDays(range) : range ?? lastDays(30));
            else setRange(value === "all" ? null : Number(value));
          }}>
            <option value="7">Last 7 days</option>
            <option value="30">Last 30 days</option>
            <option value="90">Last 90 days</option>
            <option value="all">All time</option>
            <option value="custom">Custom date range</option>
          </select>
        </div>
        <div className="flex items-center gap-2 text-sm font-semibold">
          <label htmlFor="dashboard-source">Case source</label>
          <select id="dashboard-source" value={source} onChange={event => setSource(event.target.value)}>
            <option value="">All sources</option>
            {Object.entries(sourceLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </div>
        {preset === "custom" && <form className="actions" onSubmit={event => {
          event.preventDefault();
          if (draft.start && draft.end && draft.start <= draft.end) setRange(draft);
        }}>
          <label className="muted">From <input type="date" required className="rounded border p-1"
            value={draft.start} max={draft.end || undefined}
            onChange={event => setDraft(value => ({ ...value, start: event.target.value }))} /></label>
          <label className="muted">Through <input type="date" required className="rounded border p-1"
            value={draft.end} min={draft.start || undefined}
            onChange={event => setDraft(value => ({ ...value, end: event.target.value }))} /></label>
          <Button type="submit" variant="outline">Apply dates</Button>
        </form>}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2 px-5 py-3">
        <p className="muted">Dates use UTC. Cases are filtered by opening date and show their current status.
          Workflow and activity summaries use the dates when their records were saved.
          {!source && " This view includes simulated cases and standalone workflows."}</p>
        {counts && <SyncState data={counts} />}
      </div>
    </div>
    <section aria-label="Executive overview" className="mb-5">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-lg font-bold">Executive overview</h2>
        <nav aria-label="Dashboard sections" className="actions text-sm">
          <a className="record-link" href="#cases">Cases</a>
          <a className="record-link" href="#analytics">Analytics</a>
          <a className="record-link" href="#evidence-monitoring">Evidence</a>
          <a className="record-link" href="#counsel-monitoring">Counsel</a>
          <a className="record-link" href="#resolution-monitoring">Resolution</a>
          <a className="record-link" href="#activity">Activity</a>
        </nav>
      </div>
      <dl className="dashboard-metrics">
        <Metric label="Total disputes" value={counts?.total} pending={overview.isPending} />
        <Metric label="Open disputes" value={counts?.open} pending={overview.isPending}
          hint={counts ? `${counts.unlinked} ${counts.unlinked === 1 ? "case" : "cases"} without a matched payment` : undefined} />
        <Metric label="Resolved disputes" value={counts?.resolved} pending={overview.isPending} />
        <Metric label="Awaiting merchant response" value={counts?.awaiting_merchant} pending={overview.isPending}
          hint={counts ? `${counts.overdue} overdue · ${counts.due_soon} due within 72h across active cases` : undefined} />
        <Metric label="Total disputed amount" value={counts && (counts.amounts.length ? <ul>
          {counts.amounts.map(amount => <li key={amount.currency}>{formatAmount(amount.amount_minor, amount.currency)}</li>)}
        </ul> : "No recorded amounts")} pending={overview.isPending}
          hint={counts?.unknown_amounts ? `${counts.unknown_amounts} cases with amount or currency unavailable` : undefined} />
        <Metric label="Evidence packs generated" value={evidenceStats?.packs_generated} pending={evidence.isPending}
          hint={evidenceStats ? "Saved versions in the period" : undefined} />
        <Metric label="Evidence verification success rate" value={evidenceStats && percent(evidenceStats.verification_rate)}
          pending={evidence.isPending} hint={evidenceStats ? "Verified share of checks with a pass or fail result" : undefined} />
        <Metric label="AI recommendations generated" value={counselStats?.recommendations} pending={counsel.isPending} />
        <Metric label="Pending human reviews" value={counselStats?.pending_reviews} pending={counsel.isPending}
          hint={counselStats ? "Latest recommendation per case or standalone payment" : undefined} />
      </dl>
    </section>
    <DashboardCases key={query} scope={query} statuses={counts?.by_status ?? []} reasons={counts?.by_reason ?? []} />
    <Panel title="Dispute analytics" id="analytics">
      <Remote result={overview} label="Dispute analytics">{data => <DashboardAnalytics data={data} />}</Remote>
    </Panel>
    <div className="dashboard-columns dashboard-monitoring">
      <EvidenceMonitoring result={evidence} />
      <CounselMonitoring result={counsel} />
    </div>
    <Panel title="Resolution monitoring" id="resolution-monitoring"
      detail={<Link className="record-link text-sm" href="/resolution">Open Resolution Center</Link>}>
      <Remote result={resolution} label="Resolution monitoring">{data => <ResolutionStats data={data} />}</Remote>
    </Panel>
    <DashboardActivity scope={query} />
  </>;
}
