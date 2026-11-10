"use client";

import { Suspense, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, Scale } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Panel, Remote, Skeleton, WorkflowBadge } from "@/components/dashboard-panels";
import { Empty } from "@/components/dispute-states";
import { useResolutionSession } from "@/components/resolution-session";
import { ResolutionStats } from "@/components/resolution-stats";
import { Button } from "@/components/ui/button";
import { formatAmount, formatDate, humanize, type Page } from "@/lib/disputes";
import { statuses, type Stats, type Workflow, type WorkflowRow } from "@/lib/resolution";

const columns = [
  ["paypal_dispute_id", "PayPal case"], ["payment_id", "Payment"], ["reason", "Reason"],
  ["amount_minor", "Amount"], ["recommendation", "Recommendation"], ["strategy", "Strategy"],
  ["status", "Status"], ["assigned_reviewer", "Reviewer"], ["deadline", "Deadline"], ["updated_at", "Updated"],
] as const;
type Field = typeof columns[number][0];

function Queue() {
  const { actor, cacheKey, call } = useResolutionSession();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [disputeId, setDisputeId] = useState(searchParams.get("dispute_id") ?? "");
  const [input, setInput] = useState("");
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [strategy, setStrategy] = useState("");
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState<{ field: Field; order: "asc" | "desc" }>({ field: "updated_at", order: "desc" });
  const params = new URLSearchParams({ page: String(page), sort: sort.field, order: sort.order });
  if (search) params.set("search", search);
  if (status) params.set("status", status);
  if (strategy) params.set("strategy", strategy);
  const stats = useQuery({
    queryKey: ["resolution", cacheKey, "overview"], queryFn: ({ signal }) => call<Stats>("/overview", { signal }),
  });
  const queue = useQuery({
    queryKey: ["resolution", cacheKey, "queue", params.toString()],
    queryFn: ({ signal }) => call<Page<WorkflowRow>>(`/workflows?${params}`, { signal }),
  });
  const open = useMutation({
    mutationFn: (id: number) => call<Workflow>("/workflows", {
      method: "POST", body: JSON.stringify({ dispute_id: id }),
    }),
    onSuccess: result => router.push(`/resolution/${result.id}`),
  });
  const local = process.env.NODE_ENV === "development";
  return <>
    <div className="page-heading"><div>
      <p className="eyebrow"><Scale size={15} aria-hidden="true" /> SYSTEM 3 · RESOLUTION</p>
      <h1>Resolution Center</h1>
      <p>Review recommendations, approve a package, and track Sandbox submissions.</p>
    </div><Button asChild variant="outline"><Link href="/disputes">Find a dispute</Link></Button></div>
    <Panel title="Resolution overview">
      <Remote result={stats} label="Resolution overview">{data => <ResolutionStats data={data} />}</Remote>
    </Panel>
    <Panel title="Resolution queue">
      {local && actor.role !== "viewer" && <form className="filters" onSubmit={event => {
        event.preventDefault();
        if (/^[1-9]\d{0,9}$/.test(disputeId)) open.mutate(Number(disputeId));
      }}>
        <label className="text-sm font-semibold" htmlFor="resolution-case">Gawah dispute record ID</label>
        <input id="resolution-case" className="w-36 rounded border px-3 py-2" inputMode="numeric"
          maxLength={10} value={disputeId} onChange={event => setDisputeId(event.target.value)} required />
        <Button type="submit" disabled={open.isPending || !/^[1-9]\d{0,9}$/.test(disputeId)}>
          {open.isPending ? "Opening…" : "Open workflow"}
        </Button>
        {open.isError && <p className="notice red m-0" role="alert">{open.error.message}</p>}
      </form>}
      <form className="filters" onSubmit={event => {
        event.preventDefault(); setSearch(input.trim()); setPage(1);
      }}>
        <label className="search-field"><span className="sr-only">Search resolution queue</span>
          <input placeholder="Search case or payment ID" maxLength={255} value={input}
            onChange={event => setInput(event.target.value)} />
        </label>
        <Button type="submit" variant="outline">Search</Button>
        <select aria-label="Workflow status" value={status}
          onChange={event => { setStatus(event.target.value); setPage(1); }}>
          <option value="">All statuses</option>
          {statuses.map(value => <option key={value} value={value}>{humanize(value)}</option>)}
        </select>
        <select aria-label="Resolution strategy" value={strategy}
          onChange={event => { setStrategy(event.target.value); setPage(1); }}>
          <option value="">All strategies</option>
          {["DEFEND", "REMEDY", "INFORMATION"].map(value => <option key={value} value={value}>{humanize(value)}</option>)}
        </select>
      </form>
      <Remote result={queue} label="Resolution queue">{data => <>
        {data.items.length === 0 ? <Empty title="No matching workflows"
          message="Open a workflow from a linked dispute with a current Counsel analysis." /> :
          <div className="table-scroll" tabIndex={0} role="region" aria-label="Scrollable resolution table">
            <table className="history-table dashboard-case-table">
              <thead><tr>{columns.map(([field, label]) => <th scope="col" key={field}
                aria-sort={sort.field === field ? sort.order === "asc" ? "ascending" : "descending" : undefined}>
                <button className="text-left" type="button" onClick={() => {
                  setSort({ field, order: sort.field === field && sort.order === "asc" ? "desc" : "asc" });
                  setPage(1);
                }}>{label}{sort.field === field ? sort.order === "asc" ? " ↑" : " ↓" : ""}</button>
              </th>)}</tr></thead>
              <tbody>{data.items.map(row => <tr key={row.id}>
                <td><Link className="record-link" href={`/resolution/${row.id}`}>{row.paypal_dispute_id}</Link></td>
                <td>{row.payment_id}</td><td>{humanize(row.reason)}</td><td>{formatAmount(row.amount_minor, row.currency)}</td>
                <td>{humanize(row.recommendation)}</td><td>{humanize(row.strategy)}</td>
                <td><WorkflowBadge status={row.status} /></td><td>{row.assigned_reviewer ?? "Unassigned"}</td>
                <td>{formatDate(row.deadline, true)}</td><td>{formatDate(row.updated_at, true)}</td>
              </tr>)}</tbody>
            </table>
          </div>}
        <div className="pagination">
          <span aria-live="polite">Page {page} · {data.total} workflows</span>
          <Button variant="outline" aria-label="Previous resolution page" disabled={page === 1 || queue.isFetching}
            onClick={() => setPage(value => value - 1)}><ChevronLeft size={16} /></Button>
          <Button variant="outline" aria-label="Next resolution page" disabled={page * data.page_size >= data.total || queue.isFetching}
            onClick={() => setPage(value => value + 1)}><ChevronRight size={16} /></Button>
        </div>
      </>}</Remote>
    </Panel>
  </>;
}

export default function ResolutionPage() {
  return <Suspense fallback={<Skeleton label="Loading resolution queue…" />}><Queue /></Suspense>;
}
