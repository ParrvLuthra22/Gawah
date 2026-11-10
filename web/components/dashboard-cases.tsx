"use client";

import { useState } from "react";
import { ChevronDown, ChevronLeft, ChevronRight, ChevronUp, Search } from "lucide-react";
import Link from "next/link";
import { Panel, Remote, WorkflowBadge } from "@/components/dashboard-panels";
import { Empty, Status } from "@/components/dispute-states";
import { Button } from "@/components/ui/button";
import { activeStatuses, type CasePage, type CaseRow, type Group, useDashboard, workflowHref } from "@/lib/dashboard";
import { formatAmount, formatDate, humanize, linkLabels, sourceLabels } from "@/lib/disputes";

const columns = [
  ["paypal_dispute_id", "PayPal case"],
  ["payment_id", "Payment"],
  ["reason", "Reason"],
  ["amount_minor", "Disputed amount"],
  ["status", "Status"],
  ["evidence_status", "Evidence pack"],
  ["counsel_status", "AI Counsel"],
  ["created_at", "Opened"],
  ["deadline", "Response deadline"],
  ["updated_at", "Last updated"],
] as const;

type Sort = typeof columns[number][0];

function Deadline({ row, asOf }: { row: CaseRow; asOf: string }) {
  if (!row.deadline) return <span className="muted">Unavailable</span>;
  const hoursLeft = (Date.parse(row.deadline) - Date.parse(asOf)) / 3_600_000;
  const alert = activeStatuses.includes(row.status) && hoursLeft <= 72;
  return <>
    <time dateTime={row.deadline}>{formatDate(row.deadline, true)}</time>
    {alert && <span className={`status-pill mt-1 ${hoursLeft < 0 ? "red" : "amber"}`}>
      {hoursLeft < 0 ? "Overdue" : "Due within 72h"}
    </span>}
  </>;
}

function Workflow({ row, module }: { row: CaseRow; module: "evidence" | "counsel" }) {
  const status = module === "evidence" ? row.evidence_status : row.counsel_status;
  const version = module === "evidence" ? row.evidence_version : row.counsel_version;
  return <div className="space-y-1">
    <WorkflowBadge status={status} />
    {row.payment_id && version !== null && <Link className="record-link block"
      href={workflowHref(module, row.payment_id, row.id, module === "evidence" ? version : null)}>
      {module === "evidence" ? `View version ${version}` : `View analysis · v${version}`}
    </Link>}
    {row.payment_id && status === "NOT_ASSEMBLED" && <Link className="record-link block"
      href={workflowHref("evidence", row.payment_id, row.id)}>Open evidence</Link>}
    {row.payment_id && row.evidence_version !== null && status === "NOT_ANALYZED"
      && <Link className="record-link block" href={workflowHref("counsel", row.payment_id, row.id)}>Open AI Counsel</Link>}
  </div>;
}

function options(groups: Group[]) {
  return groups.flatMap(group => group.label ? [group.label] : []).sort();
}

export function DashboardCases({ scope, statuses, reasons }: {
  scope: string; statuses: Group[]; reasons: Group[];
}) {
  const [input, setInput] = useState("");
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [reason, setReason] = useState("");
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(25);
  const [sort, setSort] = useState<{ field: Sort; order: "asc" | "desc" }>({ field: "created_at", order: "desc" });
  const query = new URLSearchParams(scope);
  query.set("page", String(page));
  query.set("page_size", String(size));
  query.set("sort", sort.field);
  query.set("order", sort.order);
  if (search) query.set("search", search);
  if (status) query.set("status", status);
  if (reason) query.set("reason", reason);
  const result = useDashboard<CasePage>("cases", query.toString());

  return <Panel title="Dispute monitoring" id="cases" detail={<Link className="record-link text-sm" href="/disputes">Open dispute console</Link>}>
    <form className="filters" onSubmit={event => {
      event.preventDefault();
      setSearch(input.trim());
      setPage(1);
    }}>
      <label className="search-field">
        <Search size={16} aria-hidden="true" />
        <span className="sr-only">Search case or payment ID</span>
        <input placeholder="Case or payment ID" maxLength={255} value={input}
          onChange={event => setInput(event.target.value)} />
      </label>
      <Button variant="outline" type="submit">Search</Button>
      <select aria-label="Case status" value={status}
        onChange={event => { setStatus(event.target.value); setPage(1); }}>
        <option value="">All statuses</option>
        {[...new Set([...activeStatuses, "RESOLVED", ...options(statuses), ...(status ? [status] : [])])]
          .map(value => <option key={value} value={value}>{humanize(value)}</option>)}
      </select>
      <select aria-label="Dispute reason" value={reason}
        onChange={event => { setReason(event.target.value); setPage(1); }}>
        <option value="">All reasons</option>
        {[...new Set([...options(reasons), ...(reason ? [reason] : [])])]
          .map(value => <option key={value} value={value}>{humanize(value)}</option>)}
      </select>
    </form>
    <Remote result={result} label="Disputes">{data => <>
      {!!data.unavailable.length && <p className="notice amber m-4" role="status">
        {data.unavailable.map(humanize).join(" and ")} status is unavailable.{" "}
        <button type="button" className="record-link" onClick={() => void result.refetch()}>Retry workflow status</button>
      </p>}
      {data.items.length === 0
        ? <Empty title="No disputes match this view" message="Change the period, source, or case filters to see more records." />
        : <div className="table-scroll" tabIndex={0} role="region" aria-label="Scrollable dispute table">
          <table className="history-table dashboard-case-table">
            <caption className="sr-only">Cases with evidence and AI Counsel status</caption>
            <thead><tr>{columns.map(([field, label]) => <th key={field} scope="col"
              aria-sort={sort.field === field ? sort.order === "asc" ? "ascending" : "descending" : undefined}>
              <button type="button" className="flex min-h-7 items-center gap-1 text-left"
                title={field === "amount_minor" ? "Sort within each currency" : undefined}
                onClick={() => {
                  setSort({ field, order: sort.field === field && sort.order === "asc" ? "desc" : "asc" });
                  setPage(1);
                }}>
                {label}{sort.field === field && (sort.order === "asc" ? <ChevronUp size={14} aria-hidden="true" />
                  : <ChevronDown size={14} aria-hidden="true" />)}
              </button>
            </th>)}</tr></thead>
            <tbody>{data.items.map(row => <tr key={row.id}>
              <td><Link className="record-link" href={`/disputes/${row.id}`}>{row.paypal_dispute_id}</Link>
                <span className="muted block">{sourceLabels[row.source]}</span></td>
              <td>{row.payment_id ?? <span className="muted">Unavailable</span>}
                {row.link_status !== "LINKED" && <span className="muted block">{linkLabels[row.link_status]}</span>}</td>
              <td>{humanize(row.reason)}</td>
              <td>{formatAmount(row.amount_minor, row.currency)}</td>
              <td><Status value={row.status} /></td>
              <td><Workflow row={row} module="evidence" /></td>
              <td><Workflow row={row} module="counsel" /></td>
              <td>{formatDate(row.created_at)}</td>
              <td><Deadline row={row} asOf={data.as_of} /></td>
              <td>{formatDate(row.updated_at, true)}</td>
            </tr>)}</tbody>
          </table>
        </div>}
      <div className="pagination">
        <label>Rows per page
          <select aria-label="Rows per page" value={size} onChange={event => { setSize(Number(event.target.value)); setPage(1); }}>
            {[10, 25, 50, 100].map(value => <option key={value}>{value}</option>)}
          </select>
        </label>
        <span aria-live="polite">{data.items.length
          ? `${(data.page - 1) * data.page_size + 1}–${(data.page - 1) * data.page_size + data.items.length} of ${data.total}`
          : `0 shown · ${data.total} cases`}</span>
        <Button variant="outline" aria-label="Previous case page" disabled={page <= 1 || result.isFetching}
          onClick={() => setPage(value => value - 1)}><ChevronLeft size={16} aria-hidden="true" /></Button>
        <Button variant="outline" aria-label="Next case page" disabled={page * size >= data.total || result.isFetching}
          onClick={() => setPage(value => value + 1)}><ChevronRight size={16} aria-hidden="true" /></Button>
      </div>
    </>}</Remote>
  </Panel>;
}
