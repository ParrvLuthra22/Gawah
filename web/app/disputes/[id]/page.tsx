"use client";

import { Suspense, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Clock3, CreditCard, Users } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { Empty, Failed, Pending, Status } from "@/components/dispute-states";
import { Button } from "@/components/ui/button";
import {
  api, type DisputeDetail, type Party, formatAmount, formatDate, humanize, linkLabels, sourceLabels,
} from "@/lib/disputes";

function Item({ label, children }: { label: string; children: ReactNode }) {
  return <div><dt>{label}</dt><dd>{children ?? "—"}</dd></div>;
}

function PartyDetails({ party }: { party: Party | null }) {
  if (!party) return <p className="muted">PayPal did not provide these details.</p>;
  return (
    <dl className="data-grid compact">
      <Item label="Name">{party.name}</Item>
      <Item label="Email">{party.email}</Item>
      {party.merchant_id && <Item label="Merchant ID">{party.merchant_id}</Item>}
    </dl>
  );
}

function Detail() {
  const { id } = useParams<{ id: string }>();
  const valid = /^[1-9]\d{0,9}$/.test(id);
  const query = useQuery({
    queryKey: ["disputes", "detail", id],
    queryFn: ({ signal }) => api<DisputeDetail>(`/disputes/${id}`, { signal }),
    enabled: valid,
  });
  if (!valid) return <Failed error={new Error("Dispute not found.")} />;
  if (query.isPending) return <Pending message="Loading dispute…" />;
  if (query.isError) return <Failed error={query.error} retry={() => void query.refetch()} />;
  const dispute = query.data;
  return (
    <>
      <div className="page-heading detail-heading">
        <div>
          <p className="eyebrow">DISPUTE DETAILS</p>
          <h1>{dispute.paypal_dispute_id}</h1>
          <p>{humanize(dispute.reason)} · {sourceLabels[dispute.source]}</p>
        </div>
        <div className="actions">
          <Status value={dispute.status} />
          {dispute.payment_id && <Button asChild variant="outline">
            <Link href={`/evidence?${new URLSearchParams({
              payment_id: dispute.payment_id, dispute_id: String(dispute.id),
            })}`}>View evidence</Link>
          </Button>}
        </div>
      </div>
      <div className="detail-layout">
        <section className="panel">
          <div className="panel-heading"><h2><CreditCard size={18} /> Dispute and payment</h2></div>
          <dl className="data-grid">
            <Item label="Disputed amount">{formatAmount(dispute.amount_minor, dispute.currency)}</Item>
            <Item label="Status"><Status value={dispute.status} /></Item>
            <Item label="Gawah payment ID">{dispute.payment_id ?? linkLabels[dispute.link_status]}</Item>
            <Item label="PayPal transaction ID">{dispute.transaction_id}</Item>
            <Item label="Opened">{formatDate(dispute.created_at, true)}</Item>
            <Item label="Updated">{formatDate(dispute.updated_at, true)}</Item>
            <Item label="Response deadline">{formatDate(dispute.deadline, true)}</Item>
            <Item label="Last synchronized">{formatDate(dispute.last_synced_at, true)}</Item>
          </dl>
        </section>
        <section className="panel">
          <div className="panel-heading"><h2><Users size={18} /> Parties</h2></div>
          <div className="party-details">
            <h3>Buyer</h3>
            <PartyDetails party={dispute.buyer} />
            <h3>Merchant</h3>
            <PartyDetails party={dispute.merchant} />
          </div>
        </section>
      </div>
      <section className="panel">
        <div className="panel-heading"><h2><Clock3 size={18} /> Status history</h2></div>
        {dispute.status_history.length === 0
          ? <Empty title="No status history" message="Status changes appear here after synchronization." />
          : <ol className="timeline">{dispute.status_history.map((change, index) => (
            <li key={`${change.changed_at}-${index}`}>
              <div className="timeline-change">
                {change.previous_status && <><Status value={change.previous_status} /><span aria-hidden="true">→</span></>}
                <Status value={change.new_status} />
              </div>
              <p>{formatDate(change.changed_at, true)} · {sourceLabels[change.source]}</p>
            </li>
          ))}</ol>}
      </section>
    </>
  );
}

export default function DisputePage() {
  return (
    <>
      <Button asChild variant="outline" className="mb-5">
        <Link href="/disputes"><ArrowLeft size={15} aria-hidden="true" /> Back to disputes</Link>
      </Button>
      <Suspense fallback={<Pending message="Loading dispute…" />}>
        <Detail />
      </Suspense>
    </>
  );
}
