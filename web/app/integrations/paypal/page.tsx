"use client";

import { useQuery } from "@tanstack/react-query";
import { Cable, RotateCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Empty, Failed, Pending, Status, SyncButton, SyncFeedback, useSync } from "@/components/dispute-states";
import { api, formatDate, type Page, type PayPalStatus, type SyncResult } from "@/lib/disputes";

export default function PayPalPage() {
  const connection = useQuery({
    queryKey: ["paypal", "status"],
    queryFn: ({ signal }) => api<PayPalStatus>("/paypal/status", { signal }),
  });
  const history = useQuery({
    queryKey: ["paypal", "history"],
    queryFn: ({ signal }) => api<Page<SyncResult>>("/disputes/sync/history?page_size=10", { signal }),
  });
  const sync = useSync();
  const status = connection.data;

  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow"><Cable size={15} aria-hidden="true" /> INTEGRATION</p>
          <h1>PayPal Sandbox</h1>
          <p>Check dispute API access and review synchronization results.</p>
        </div>
        <div className="actions">
          <Button variant="outline" disabled={connection.isFetching || history.isFetching}
            onClick={() => { void connection.refetch(); void history.refetch(); }}>
            <RotateCw size={15} aria-hidden="true" /> Refresh
          </Button>
          <SyncButton sync={sync} />
        </div>
      </div>
      <SyncFeedback sync={sync} />
      <section className="panel">
        <div className="panel-heading"><h2>Connection</h2></div>
        {connection.isPending ? <Pending message="Checking PayPal connection…" />
          : connection.isError ? <Failed error={connection.error} retry={() => void connection.refetch()} />
          : status && (
            <>
              <dl className="data-grid integration-data">
                <div><dt>Sandbox connection</dt><dd><Status value={status.connection} /></dd></div>
                <div><dt>API credentials</dt><dd>{status.credentials_configured ? "Configured" : "Missing"}</dd></div>
                <div><dt>PayPal disputes stored</dt><dd>{status.total_disputes}</dd></div>
                <div><dt>Last sync attempt</dt><dd>{status.last_sync
                  ? <>{formatDate(status.last_sync.finished_at, true)} <Status value={status.last_sync.result} /></>
                  : "No sync attempts yet"}</dd></div>
              </dl>
              {status.error && <div className="notice red panel-notice" role="alert">{status.error}</div>}
            </>
          )}
      </section>
      <section className="panel">
        <div className="panel-heading"><h2>Recent sync runs</h2></div>
        {history.isPending ? <Pending message="Loading sync history…" />
          : history.isError ? <Failed error={history.error} retry={() => void history.refetch()} />
          : history.data.items.length === 0
            ? <Empty title="No sync attempts yet" message="Run a sync to view its results here." />
            : (
              <div className="table-scroll">
                <table className="history-table">
                  <thead><tr><th scope="col">Started</th><th scope="col">Result</th><th scope="col">Retrieved</th>
                    <th scope="col">New</th><th scope="col">Updated</th><th scope="col">Unlinked</th>
                    <th scope="col">Errors</th></tr></thead>
                  <tbody>{history.data.items.map(run => (
                    <tr key={run.id}><td>{formatDate(run.started_at, true)}</td><td><Status value={run.result} /></td>
                      <td>{run.retrieved}</td><td>{run.created}</td><td>{run.updated}</td><td>{run.unmatched}</td>
                      <td>{run.errors.join(" ") || "—"}</td></tr>
                  ))}</tbody>
                </table>
              </div>
            )}
      </section>
    </>
  );
}
