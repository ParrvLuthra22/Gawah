"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AgGridReact } from "ag-grid-react";
import { AllCommunityModule, themeQuartz, type ColDef, type ICellRendererParams } from "ag-grid-community";
import { ChevronLeft, ChevronRight, ListChecks, RotateCw, Search } from "lucide-react";
import Link from "next/link";
import {
  api, type Dispute, type Page, formatAmount, formatDate, humanize, linkLabels, sourceLabels,
} from "@/lib/disputes";
import { Empty, Failed, Pending, Status, SyncButton, SyncFeedback, useSync } from "@/components/dispute-states";
import { Button } from "@/components/ui/button";

const modules = [AllCommunityModule];
const theme = themeQuartz.withParams({
  accentColor: "#0069d3", headerBackgroundColor: "#f8faff", borderColor: "#e5edf5",
  foregroundColor: "#263952", headerTextColor: "#38516f", fontSize: 13, rowHeight: 51,
  headerHeight: 43, wrapperBorder: false, wrapperBorderRadius: 0, fontFamily: "inherit",
});

const baseColumns: ColDef<Dispute>[] = [
  { field: "paypal_dispute_id", headerName: "Dispute ID", minWidth: 170, flex: 1,
    cellRenderer: ({ data }: ICellRendererParams<Dispute>) => data &&
      <Link className="record-link" href={`/disputes/${data.id}`}>{data.paypal_dispute_id}</Link> },
  { field: "payment_id", headerName: "Payment ID", minWidth: 138,
    valueFormatter: ({ data }) => data ? data.payment_id ?? linkLabels[data.link_status] : "—" },
  { field: "reason", minWidth: 230, flex: 1.6, valueFormatter: ({ value }) => humanize(value) },
  { field: "amount_minor", headerName: "Amount", minWidth: 120,
    valueFormatter: ({ data }) => data ? formatAmount(data.amount_minor, data.currency) : "—" },
  { field: "status", minWidth: 145, cellRenderer: ({ value }: ICellRendererParams<Dispute>) =>
    <Status value={value} /> },
  { field: "created_at", headerName: "Created", minWidth: 118, valueFormatter: ({ value }) => formatDate(value) },
  { field: "updated_at", headerName: "Updated", minWidth: 118, valueFormatter: ({ value }) => formatDate(value) },
  { field: "deadline", minWidth: 118, valueFormatter: ({ value }) => formatDate(value) },
  { field: "source", minWidth: 148, valueFormatter: ({ data }) => data ? sourceLabels[data.source] : "—" },
];

const defaultColDef: ColDef<Dispute> = {
  sortable: true, sortingOrder: ["asc", "desc"], comparator: () => 0, width: 140,
};
const statuses = ["OPEN", "WAITING_FOR_BUYER_RESPONSE", "WAITING_FOR_SELLER_RESPONSE", "UNDER_REVIEW", "RESOLVED", "OTHER"];

export default function DisputesPage() {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [source, setSource] = useState("");
  const [link, setLink] = useState("");
  const [sort, setSort] = useState({ field: "created_at", direction: "desc" as "asc" | "desc" });
  const columns = useMemo(() => baseColumns.map(column => ({
    ...column, initialSort: column.field === sort.field ? sort.direction : undefined,
  })), [sort]);
  const query = new URLSearchParams({
    page: String(page), page_size: String(pageSize), sort: sort.field, order: sort.direction,
  });
  if (search.trim()) query.set("search", search.trim());
  if (status) query.set("status", status);
  if (source) query.set("source", source);
  if (link) query.set("link_status", link);
  const list = useQuery({
    queryKey: ["disputes", "list", query.toString()],
    queryFn: ({ signal }) => api<Page<Dispute>>(`/disputes?${query}`, { signal }),
  });
  const sync = useSync();
  const total = list.data?.total ?? 0;
  const filtered = Boolean(search.trim() || status || source || link);
  const start = total && list.data?.items.length ? (page - 1) * pageSize + 1 : 0;
  const end = start ? start + (list.data?.items.length ?? 0) - 1 : 0;

  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow"><ListChecks size={15} aria-hidden="true" /> CASE MANAGEMENT</p>
          <h1>Disputes</h1>
          <p>Review PayPal Sandbox cases and their Gawah payment references.</p>
        </div>
        <div className="actions">
          <Button variant="outline" disabled={list.isFetching} onClick={() => void list.refetch()}>
            <RotateCw size={15} aria-hidden="true" /> Refresh
          </Button>
          <SyncButton sync={sync} />
        </div>
      </div>
      <SyncFeedback sync={sync} />
      <section className="panel" aria-labelledby="case-list-title">
        <div className="panel-heading">
          <h2 id="case-list-title">Case list</h2>
          {list.data && <span className="total-label">{total} {total === 1 ? "case" : "cases"}</span>}
        </div>
        <div className="filters">
          <label className="search-field">
            <span className="sr-only">Search disputes</span><Search size={17} aria-hidden="true" />
            <input value={search} maxLength={255} placeholder="Search ID, payment or reason"
              onChange={event => { setSearch(event.target.value); setPage(1); }} />
          </label>
          <select aria-label="Status" value={status} onChange={event => { setStatus(event.target.value); setPage(1); }}>
            <option value="">All statuses</option>
            {statuses.map(value => <option value={value} key={value}>{humanize(value)}</option>)}
          </select>
          <select aria-label="Source" value={source} onChange={event => { setSource(event.target.value); setPage(1); }}>
            <option value="">All sources</option>
            {Object.entries(sourceLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
          </select>
          <select aria-label="Payment link" value={link} onChange={event => { setLink(event.target.value); setPage(1); }}>
            <option value="">All payment links</option>
            {Object.entries(linkLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
          </select>
        </div>
        {list.isPending ? <Pending message="Loading disputes…" />
          : list.isError ? <Failed error={list.error} retry={() => void list.refetch()} />
          : list.data.items.length === 0 ? (
            <Empty title={filtered ? "No matching disputes" : "No disputes to display"}
              message={filtered ? "Try a different search or filter." : "Sync PayPal to retrieve available Sandbox cases."} />
          ) : (
            <div className="case-grid">
              <AgGridReact<Dispute> modules={modules} theme={theme} rowData={list.data.items}
                columnDefs={columns} defaultColDef={defaultColDef}
                getRowId={({ data }) => String(data.id)} suppressMultiSort
                onSortChanged={({ api }) => {
                  const selected = api.getColumnState().find(column => column.sort);
                  if (selected?.sort && (selected.colId !== sort.field || selected.sort !== sort.direction)) {
                    setSort({ field: selected.colId, direction: selected.sort });
                    setPage(1);
                  }
                }} />
            </div>
          )}
        <div className="pagination">
          <label>Rows per page
            <select aria-label="Rows per page" value={pageSize}
              onChange={event => { setPageSize(Number(event.target.value)); setPage(1); }}>
              {[10, 25, 50, 100].map(size => <option key={size}>{size}</option>)}
            </select>
          </label>
          <span aria-live="polite">{start ? `${start}–${end} of ${total}` : `0 of ${total}`}</span>
          <div className="flex gap-1">
            <Button variant="outline" aria-label="Previous page" className="px-2" disabled={page === 1 || list.isFetching}
              onClick={() => setPage(page - 1)}><ChevronLeft size={16} /></Button>
            <Button variant="outline" aria-label="Next page" className="px-2" disabled={page * pageSize >= total || list.isFetching}
              onClick={() => setPage(page + 1)}><ChevronRight size={16} /></Button>
          </div>
        </div>
      </section>
    </>
  );
}
