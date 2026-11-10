"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AgGridReact } from "ag-grid-react";
import { AllCommunityModule, themeQuartz, type ColDef } from "ag-grid-community";
import { Play, Search } from "lucide-react";
import { Panel, Remote } from "@/components/dashboard-panels";
import { Button } from "@/components/ui/button";
import { api, formatAmount, formatDate, humanize } from "@/lib/disputes";
import type { BlockedPurchaseRow, DemoScenario, PaymentLedgerRow } from "@/lib/dashboard";

const modules = [AllCommunityModule];
const theme = themeQuartz.withParams({
  accentColor: "#0069d3", headerBackgroundColor: "#f8faff", borderColor: "#e5edf5",
  foregroundColor: "#263952", headerTextColor: "#38516f", fontSize: 13, rowHeight: 47,
  headerHeight: 43, wrapperBorder: false, wrapperBorderRadius: 0, fontFamily: "inherit",
});

const paymentColumns: ColDef<PaymentLedgerRow>[] = [
  { field: "id", headerName: "Payment ID", minWidth: 190, flex: 1 },
  { field: "amount_minor", headerName: "Amount", minWidth: 130, valueFormatter: p => p.data ? formatAmount(p.data.amount_minor, p.data.currency) : "—" },
  { field: "status", minWidth: 130, valueFormatter: p => humanize(p.value) },
  { field: "proposal_id", headerName: "Proposal", minWidth: 180 },
  { field: "dispute_status", headerName: "Dispute", minWidth: 150, valueFormatter: p => humanize(p.value) },
  { field: "created_at", headerName: "Created", minWidth: 170, valueFormatter: p => formatDate(p.value, true) },
  { field: "simulated", headerName: "Demo", minWidth: 105, valueFormatter: p => p.value ? "SIMULATED" : "—" },
];
const blockedColumns: ColDef<BlockedPurchaseRow>[] = [
  { field: "proposal_id", headerName: "Purchase ID", minWidth: 190, flex: 1 },
  { field: "amount_minor", headerName: "Amount", minWidth: 130, valueFormatter: p => p.data ? formatAmount(p.data.amount_minor, p.data.currency) : "—" },
  { field: "reason", headerName: "Blocking reason", minWidth: 210, flex: 1.3, valueFormatter: p => humanize(p.value) },
  { field: "status", minWidth: 120 },
  { field: "blocked_at", headerName: "Blocked", minWidth: 170, valueFormatter: p => formatDate(p.value, true) },
];
const defaultColDef = { sortable: true, filter: true, resizable: true, flex: 1, minWidth: 110 };

function Grid<T extends { id: string }>({ rows, columns, label }: { rows: T[]; columns: ColDef<T>[]; label: string }) {
  const [search, setSearch] = useState("");
  return <>
    <div className="filters"><label className="search-field"><Search size={16} /><span className="sr-only">Search {label}</span>
      <input value={search} onChange={event => setSearch(event.target.value)} placeholder={`Search ${label}`} /></label></div>
    {rows.length ? <div className="case-grid"><AgGridReact<T> modules={modules} theme={theme} rowData={rows}
      columnDefs={columns} defaultColDef={defaultColDef} quickFilterText={search} pagination paginationPageSize={10}
      getRowId={({ data }) => data.id} /></div> : <p className="panel-state">No {label} are recorded in the System 2 ledger.</p>}
  </>;
}

export function DashboardLedger() {
  const client = useQueryClient();
  const payments = useQuery({ queryKey: ["engine", "payments"], queryFn: ({ signal }) => api<PaymentLedgerRow[]>("/dashboard/payments", { signal }) });
  const blocked = useQuery({ queryKey: ["engine", "blocked"], queryFn: ({ signal }) => api<BlockedPurchaseRow[]>("/dashboard/blocked-purchases", { signal }) });
  const demo = useMutation({
    mutationFn: () => api<DemoScenario[]>("/dashboard/demo-scenarios", { method: "POST" }),
    onSuccess: () => void client.invalidateQueries(),
  });
  const result = demo.data;
  return <>
    <Panel title="Payment ledger" id="payment-ledger" detail={<span className="muted">Live System 2 records</span>}>
      <Remote result={payments} label="Payment ledger">{rows => <Grid rows={rows} columns={paymentColumns} label="payments" />}</Remote>
    </Panel>
    <Panel title="Blocked purchases" id="blocked-purchases" detail={<span className="muted">Derived from signed System 2 decisions</span>}>
      <Remote result={blocked} label="Blocked purchases">{rows => <Grid rows={rows} columns={blockedColumns} label="blocked purchases" />}</Remote>
    </Panel>
    <div className="panel p-5">
      <div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="font-bold">Demo scenarios</h2><p className="muted mt-1">Creates internal, simulated cases from two paid System 2 records. No PayPal dispute, reply, or refund is submitted.</p></div>
        <Button disabled={demo.isPending} onClick={() => demo.mutate()}><Play size={15} /> {demo.isPending ? "Running scenarios…" : "Run Demo Scenarios"}</Button></div>
      {demo.isError && <p className="notice red mt-4">{demo.error.message}</p>}
      {result && <ul className="dashboard-records mt-4">{result.map(item => <li key={item.scenario}><strong>{item.scenario}</strong> · {item.status} · payment {item.payment_id}
        {item.recommendation && ` · Counsel: ${humanize(item.recommendation)}`}{item.workflow_id && ` · workflow #${item.workflow_id}`}
        {item.detail && <span className="muted block">{item.detail}</span>}</li>)}</ul>}
    </div>
  </>;
}
