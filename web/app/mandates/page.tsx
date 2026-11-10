"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Empty, Failed, Pending, Status } from "@/components/dispute-states";
import { Button } from "@/components/ui/button";
import { api, formatAmount, formatDate } from "@/lib/disputes";

type Mandate = {
  id: string; user_id: string; category: string; item_description: string;
  status: string; issued_at: string; expires_at: string; max_amount_minor: number;
  currency: string; allowed_merchants: string[]; key_id: string; signature: string;
};
type Budget = { currency: string; spent_minor: number; reserved_minor: number; remaining_minor: number };

function MandateDetail({ id }: { id: string }) {
  const mandate = useQuery({
    queryKey: ["mandates", id],
    queryFn: ({ signal }) => api<Mandate>(`/mandates/${id}`, { signal }),
  });
  const budget = useQuery({
    queryKey: ["mandates", id, "budget"],
    queryFn: ({ signal }) => api<Budget>(`/mandates/${id}/budget`, { signal }),
  });
  return <section className="panel">
    <div className="panel-heading"><h2>Mandate {id}</h2></div>
    {mandate.isPending ? <Pending message="Loading mandate…" />
      : mandate.isError ? <Failed error={mandate.error} retry={() => void mandate.refetch()} />
      : <dl className="data-grid">
        <div><dt>Authorized purchase</dt><dd>{mandate.data.item_description}</dd></div>
        <div><dt>Category</dt><dd>{mandate.data.category}</dd></div>
        <div><dt>User</dt><dd>{mandate.data.user_id}</dd></div>
        <div><dt>Allowed merchants</dt><dd>{mandate.data.allowed_merchants.join(", ") || "—"}</dd></div>
        <div><dt>Issued</dt><dd>{formatDate(mandate.data.issued_at, true)}</dd></div>
        <div><dt>Expires</dt><dd>{formatDate(mandate.data.expires_at, true)}</dd></div>
        <div><dt>Signing key</dt><dd>{mandate.data.key_id}</dd></div>
        {mandate.data.signature.startsWith("mock-signature-")
          && <div><dt>Source</dt><dd>Mock engine fixture</dd></div>}
      </dl>}
    <div className="panel-heading"><h2>Mandate budget</h2></div>
    {budget.isPending ? <Pending message="Loading budget…" />
      : budget.isError ? <Failed error={budget.error} retry={() => void budget.refetch()} />
      : <dl className="data-grid">
        <div><dt>Spent</dt><dd>{formatAmount(budget.data.spent_minor, budget.data.currency)}</dd></div>
        <div><dt>Reserved</dt><dd>{formatAmount(budget.data.reserved_minor, budget.data.currency)}</dd></div>
        <div><dt>Remaining</dt><dd>{formatAmount(budget.data.remaining_minor, budget.data.currency)}</dd></div>
      </dl>}
  </section>;
}

export default function MandatesPage() {
  const [selected, setSelected] = useState<string>();
  const mandates = useQuery({
    queryKey: ["mandates"],
    queryFn: ({ signal }) => api<Mandate[]>("/mandates", { signal }),
  });
  return <>
    <div className="page-heading"><div>
      <p className="eyebrow">CENTRAL DASHBOARD</p><h1>Mandates</h1>
      <p>Review spending authorization, allowed merchants, and remaining budget.</p>
    </div></div>
    <section className="panel">
      <div className="panel-heading"><h2>Spending mandates</h2></div>
      {mandates.isPending ? <Pending message="Loading mandates…" />
        : mandates.isError ? <Failed error={mandates.error} retry={() => void mandates.refetch()} />
        : !mandates.data.length ? <Empty title="No mandates" message="System 2 returned no spending mandates." />
        : <div className="table-scroll"><table className="w-full text-left">
          <thead><tr><th scope="col">Mandate</th><th scope="col">Purchase</th><th scope="col">Status</th><th scope="col">Spending cap</th></tr></thead>
          <tbody>{mandates.data.map(mandate => <tr key={mandate.id}>
            <td><Button variant="outline" aria-pressed={selected === mandate.id}
              onClick={() => setSelected(mandate.id)}>{mandate.id}</Button></td>
            <td>{mandate.item_description}</td><td><Status value={mandate.status} /></td>
            <td>{formatAmount(mandate.max_amount_minor, mandate.currency)}</td>
          </tr>)}</tbody>
        </table></div>}
    </section>
    {selected && <MandateDetail id={selected} />}
  </>;
}
