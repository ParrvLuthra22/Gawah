"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Empty, Failed, Pending } from "@/components/dispute-states";
import { Button } from "@/components/ui/button";
import { api, formatDate, humanize } from "@/lib/disputes";

type Entry = { seq: number; ts: string; type: string; subject_id: string; hash: string; key_id: string };
type Head = { seq: number; hash: string; message: string; signature: string; key_id: string };
type Verification = {
  ok: boolean; checked: number; first_broken_seq: number | null; reason: string | null;
  checks: { name: string; ok: boolean; detail: string }[];
};

const checkNames = ["ledger_chain", "mandate_signature", "cart_signature", "payment_reference_match"];

function VerificationPanel({ mock }: { mock: boolean | undefined }) {
  const [payment, setPayment] = useState("");
  const verify = useMutation({
    mutationFn: () => api<Verification>("/ledger/verify"),
  });
  const checks = useMutation({
    mutationFn: (id: string) => api<Verification>(`/ledger/payments/${id}/verification`),
  });
  const result = verify.data;
  const id = payment.trim();
  return <section className="panel">
    <div className="panel-heading"><h2>Ledger verification</h2></div>
    <div className="space-y-4 p-5">
      <Button onClick={() => verify.mutate()} disabled={verify.isPending}>
        {verify.isPending ? "Verifying…" : "Verify chain"}
      </Button>
      {verify.isError && <p role="alert" className="notice red">{verify.error.message}</p>}
      {result && <p role="status" className={`notice ${result.ok ? "blue" : "red"}`}>
        {result.ok ? result.checked === 0 ? "No ledger entries were checked."
          : mock === true ? "Simulated chain hashes and links passed."
          : "System 2 reported that the ledger chain passed." : "Chain verification failed."}
        {" "}{result.checked} entries checked.
        {result.first_broken_seq !== null && ` First broken entry #${result.first_broken_seq}.`}
        {result.reason && ` ${result.reason}`}
      </p>}
      <form className="actions" onSubmit={event => {
        event.preventDefault();
        if (/^[A-Za-z0-9_-]{1,255}$/.test(id)) checks.mutate(id);
      }}>
        <label className="flex flex-col gap-1">
          Payment ID for individual checks
          <input className="rounded border px-3 py-2" value={payment} maxLength={255}
            onChange={event => setPayment(event.target.value)} placeholder="Payment ID" />
        </label>
        <Button type="submit" variant="outline"
          disabled={!/^[A-Za-z0-9_-]{1,255}$/.test(id) || checks.isPending}>
          {checks.isPending ? "Checking…" : "Run payment checks"}
        </Button>
      </form>
      {checks.isError && <p role="alert" className="notice red">{checks.error.message}</p>}
      {checks.data && <>
        <p className="muted">Results for {checks.variables}. {mock === true ? "Simulated engine results."
          : mock === undefined ? "Ledger source unavailable." : ""}</p>
        <ul className="space-y-2">{checkNames.map(name => {
          const matches = checks.data?.checks.filter(check => check.name === name);
          const check = matches?.length === 1 ? matches[0] : undefined;
          return <li key={name}>
            <strong>{humanize(name)}</strong> · {check ? check.ok ? "Passed" : "Failed" : "Unavailable"}
            {check?.detail && <p className="muted">{check.detail}</p>}
          </li>;
        })}</ul>
      </>}
    </div>
  </section>;
}

function LiveLedger({ initial }: { initial: Entry[] }) {
  const [received, setReceived] = useState<Entry[]>([]);
  const [connection, setConnection] = useState("Connecting to the ledger…");
  const cursor = initial.reduce((last, entry) => Math.max(last, entry.seq), 0);
  const bySequence = new Map([...initial, ...received].map(entry => [entry.seq, entry]));
  const entries = [...bySequence.values()].sort((a, b) => b.seq - a.seq).slice(0, 100);
  useEffect(() => {
    const feed = new EventSource(`/api/ledger/stream?since_seq=${cursor}`);
    feed.onopen = () => setConnection("Live feed connected");
    feed.onerror = () => setConnection(feed.readyState === EventSource.CLOSED
      ? "Live feed unavailable. Reload to try again." : "Live feed reconnecting…");
    feed.addEventListener("ledger", (event: MessageEvent<string>) => {
      try {
        const entry: Entry = JSON.parse(event.data);
        if (!Number.isSafeInteger(entry.seq) || entry.seq < 1 || !entry.type || !entry.subject_id
            || !Number.isFinite(Date.parse(entry.ts))) return;
        setReceived(previous => previous.some(item => item.seq === entry.seq) ? previous
          : [entry, ...previous].sort((a, b) => b.seq - a.seq).slice(0, 100));
      } catch {
        setConnection("The stream returned an unreadable entry.");
      }
    });
    return () => feed.close();
  }, [cursor]);

  return <section className="panel">
    <div className="panel-heading"><h2>Live entries</h2><span className="muted" role="status">{connection}</span></div>
    {!entries.length ? <Empty title="No ledger entries" message="New entries will appear here." />
      : <div className="table-scroll"><table className="w-full text-left">
        <caption className="sr-only">Latest 100 ledger entries</caption>
        <thead><tr><th scope="col">Sequence</th><th scope="col">Event</th><th scope="col">Record</th><th scope="col">Time</th></tr></thead>
        <tbody>{entries.map(entry => <tr key={entry.seq}>
          <td>#{entry.seq}</td><td>{humanize(entry.type)}</td>
          <td>{entry.subject_id}</td><td>{formatDate(entry.ts, true)}</td>
        </tr>)}</tbody>
      </table></div>}
  </section>;
}

export default function LedgerPage() {
  const entries = useQuery({
    queryKey: ["ledger", "entries"],
    queryFn: ({ signal }) => api<Entry[]>("/ledger", { signal }),
  });
  const head = useQuery({
    queryKey: ["ledger", "head"],
    queryFn: ({ signal }) => api<Head>("/ledger/head", { signal }),
    refetchInterval: 15_000,
  });
  const mock = head.data && (head.data.key_id === "mock" || head.data.signature.startsWith("SIMULATED-"));
  return <>
    <div className="page-heading"><div>
      <p className="eyebrow">CENTRAL DASHBOARD</p><h1>Ledger</h1>
      <p>Watch System 2 ledger events and check the integrity of recorded activity.</p>
    </div></div>
    {mock && <p className="notice amber">Mock engine feed. Its ledger head signature is a simulated placeholder.</p>}
    {head.isError && <p className="notice amber">The ledger head and its source label are unavailable.</p>}
    {head.data && <section className="panel">
      <div className="panel-heading"><h2>Latest ledger head</h2></div>
      <dl className="data-grid break-all">
        <div><dt>Sequence</dt><dd>#{head.data.seq}</dd></div>
        <div><dt>Signing key</dt><dd>{head.data.key_id}</dd></div>
        <div><dt>Signed message</dt><dd>{head.data.message}</dd></div>
        <div><dt>Signature</dt><dd>{head.data.signature}</dd></div>
      </dl>
    </section>}
    <VerificationPanel mock={mock} />
    {entries.isPending ? <Pending message="Loading ledger…" />
      : entries.isError ? <Failed error={entries.error} retry={() => void entries.refetch()} />
      : <LiveLedger initial={entries.data} />}
  </>;
}
