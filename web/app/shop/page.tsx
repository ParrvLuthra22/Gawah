"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { LoaderCircle, ShoppingBag } from "lucide-react";
import { Empty, Failed, Pending, Status } from "@/components/dispute-states";
import { EvidencePanel } from "@/components/shop/evidence-panel";
import { ProductCard } from "@/components/shop/product-card";
import { Button } from "@/components/ui/button";
import { formatAmount, humanize } from "@/lib/disputes";
import {
  examplePrompts, shopApi, type Config, type ConfirmResult, type MandateSummary, type Prepared, type RecommendResult,
} from "@/lib/shop";

export default function ShopPage() {
  const config = useQuery({ queryKey: ["shop", "config"], queryFn: ({ signal }) => shopApi<Config>("config", { signal }) });
  const [message, setMessage] = useState(examplePrompts[0]);
  const [result, setResult] = useState<RecommendResult>();
  const [selected, setSelected] = useState<string>();
  const [budget, setBudget] = useState("");
  const [minutes, setMinutes] = useState("60");
  const [approved, setApproved] = useState(false);
  const [mandate, setMandate] = useState<MandateSummary>();
  const [prepared, setPrepared] = useState<Prepared>();
  const [confirmed, setConfirmed] = useState(false);
  const [paid, setPaid] = useState<ConfirmResult>();
  const [busy, setBusy] = useState<string>();
  const [error, setError] = useState<string>();

  const all = result ? [...(result.best ? [result.best] : []), ...result.ranked.filter(r => r.product.id !== result.best?.product.id)] : [];
  const chosen = all.find(r => r.product.id === selected);

  async function run<T>(name: string, fn: () => Promise<T>, done: (value: T) => void) {
    setBusy(name); setError(undefined);
    try { done(await fn()); } catch (e) { setError(e instanceof Error ? e.message : "The request failed."); } finally { setBusy(undefined); }
  }
  const search = () => run("search", () => shopApi<RecommendResult>("recommend", { method: "POST", body: { message } }), value => {
    setResult(value); setSelected(undefined); setMandate(undefined); setPrepared(undefined); setPaid(undefined); setApproved(false); setConfirmed(false);
  });
  const authorize = () => run("mandate", () => shopApi<{ mandate: MandateSummary }>("mandate", { method: "POST", body: {
    approved: true, recommendation_id: result!.recommendation_id, product_id: selected, budget, expiry_minutes: Number(minutes),
  } }), value => setMandate(value.mandate));
  const prepare = () => run("prepare", () => shopApi<Prepared>("prepare", { method: "POST", body: { recommendation_id: result!.recommendation_id } }), setPrepared);
  const total = prepared?.proposal.total_minor;
  const currency = prepared?.proposal.currency;
  const pay = () => run("confirm", () => shopApi<ConfirmResult>("confirm", { method: "POST", body: {
    prepared_id: prepared!.prepared_id, confirmed: true, expected_total_minor: total, expected_currency: currency,
  } }), setPaid);
  const disabled = config.data && !config.data.enabled;

  return <>
    <div className="page-heading"><div>
      <p className="eyebrow">AI SHOPPER</p><h1>AI Shopper</h1>
      <p>Describe what you want. The agent proposes; you authorize a mandate; the Guardian decides; PayPal Sandbox pays.</p>
    </div></div>
    {config.isError && <Failed error={config.error} />}
    <div className="notice amber" role="note"><strong>Local demo page: needs the shopper agent running.</strong></div>
    {config.data && <div className={`notice ${config.data.engine.reachable ? "blue" : "amber"}`}>
      <strong>Local demo, PayPal Sandbox only.</strong>
      <p>{config.data.engine.reachable
        ? config.data.engine.simulated
          ? "Engine reachable, in mock/test mode: payments are SIMULATED (the engine's fake PayPal)."
          : `Engine reachable: PayPal ${config.data.engine.sandbox ? "Sandbox" : "mode not reported as Sandbox"}${config.data.engine.card_configured ? "" : ", no Sandbox card configured"}.`
        : "The engine did not answer its health check."}
        {" "}This dashboard has no login, so checkout is limited to a local dev server.</p>
      {disabled && <p>Checkout is disabled: {config.data.blockers.join(" ")}</p>}
    </div>}

    <section className="panel" aria-labelledby="shop-prompt">
      <div className="panel-heading"><h2 id="shop-prompt">1. What do you want to buy?</h2></div>
      <form className="shop-form" onSubmit={e => { e.preventDefault(); void search(); }}>
        <label htmlFor="shop-message" className="muted">Shopping request</label>
        <textarea id="shop-message" rows={2} maxLength={500} value={message} onChange={e => setMessage(e.target.value)} />
        <div className="shop-chips">{examplePrompts.map(p => <Button key={p} variant="outline" onClick={() => setMessage(p)}>{p}</Button>)}</div>
        <div><Button type="submit" disabled={!!busy || message.trim().length < 3 || !!disabled}>
          {busy === "search" ? <LoaderCircle size={16} className="animate-spin" aria-hidden="true" /> : <ShoppingBag size={16} aria-hidden="true" />}
          {busy === "search" ? "Asking the shopper agent…" : "Find products"}</Button></div>
      </form>
    </section>
    {error && <div className="notice red" role="alert"><strong>{error}</strong></div>}
    {busy === "search" && <Pending message="System 1 is extracting intent and ranking products…" />}

    {result && <section className="panel" aria-labelledby="shop-results">
      <div className="panel-heading"><h2 id="shop-results">2. Recommendations</h2></div>
      <div className="notice blue panel-notice">
        <strong>Intent:</strong> {Object.entries(result.intent).filter(([, v]) => v !== null && v !== "" && !(Array.isArray(v) && !v.length))
          .map(([k, v]) => `${humanize(k)}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`).join(" · ") || "none extracted"}
        <p>{result.explanation}</p>
      </div>
      {!all.length ? <Empty title="No matching products" message="The agent found nothing that fits. Nothing was invented." />
        : <div className="shop-grid">{all.map(r => <ProductCard key={r.product.id} rec={r} best={r.product.id === result.best?.product.id}
          selected={r.product.id === selected} onSelect={id => { setSelected(id); setBudget(all.find(r => r.product.id === id)?.product.price ?? ""); setApproved(false); setMandate(undefined); setPrepared(undefined); setPaid(undefined); }} />)}</div>}
      {!!result.needs_verification.length && <div className="notice amber panel-notice"><strong>Needs verification (not purchasable)</strong>
        <ul>{result.needs_verification.map(n => <li key={n.product.id}>{n.product.title}: {n.reasons.join("; ")}</li>)}</ul></div>}
      {!!result.trace.length && <p className="muted" style={{ padding: "0 20px 16px" }}>Agent trace: {result.trace.map(t => `${t.tool} (${t.status}, ${t.result_count})`).join(" → ")}</p>}
    </section>}

    {chosen && <section className="panel" aria-labelledby="shop-mandate">
      <div className="panel-heading"><h2 id="shop-mandate">3. Approve spending restrictions</h2></div>
      {mandate ? <dl className="data-grid">
        <div><dt>Mandate</dt><dd>{mandate.id} <Status value={mandate.status} /></dd></div>
        <div><dt>Cap</dt><dd>{formatAmount(mandate.max_amount_minor, mandate.currency)}</dd></div>
        <div><dt>Merchant</dt><dd>{mandate.allowed_merchants.join(", ")}</dd></div>
        <div><dt>Signed</dt><dd>{mandate.signing_alg} · {mandate.key_id} (AP2-inspired signed mandate)</dd></div>
      </dl> : <form className="shop-form" onSubmit={e => { e.preventDefault(); void authorize(); }}>
        <p><strong>{chosen.product.title}</strong> from {chosen.product.merchant_id}, {chosen.product.currency} {chosen.product.price}.</p>
        <label htmlFor="shop-budget" className="muted">Maximum spend ({chosen.product.currency})</label>
        <input id="shop-budget" inputMode="decimal" value={budget} onChange={e => setBudget(e.target.value)} />
        <label htmlFor="shop-expiry" className="muted">Mandate expires in (minutes)</label>
        <input id="shop-expiry" inputMode="numeric" value={minutes} onChange={e => setMinutes(e.target.value)} />
        <label><input type="checkbox" checked={approved} onChange={e => setApproved(e.target.checked)} /> I authorize the agent to propose
          this purchase from {chosen.product.merchant_id} up to this amount, until it expires.</label>
        <div><Button type="submit" disabled={!approved || !!busy || !!disabled}>{busy === "mandate" ? "Signing mandate…" : "Create and sign mandate"}</Button></div>
      </form>}
    </section>}

    {mandate && <section className="panel" aria-labelledby="shop-proposal">
      <div className="panel-heading"><h2 id="shop-proposal">4. Proposal and Guardian check</h2></div>
      {!prepared ? <div className="shop-form"><p className="muted">System 1 gets the merchant-signed cart; the Guardian then runs a dry run (no payment).</p>
        <div><Button disabled={!!busy} onClick={() => void prepare()}>{busy === "prepare" ? "Preparing…" : "Prepare signed proposal"}</Button></div></div>
        : <dl className="data-grid">
          <div><dt>Guardian dry run</dt><dd><Status value={prepared.guardian.verdict === "ALLOW" ? "SUCCESS" : "FAILED"} /> {prepared.guardian.verdict}</dd></div>
          <div><dt>Total</dt><dd>{total !== undefined ? formatAmount(total, currency ?? null) : "—"}</dd></div>
          <div><dt>Reasons</dt><dd>{prepared.guardian.reasons.join(", ") || "—"}</dd></div>
          <div><dt>Explanation</dt><dd>{prepared.guardian.explanation}</dd></div>
        </dl>}
    </section>}

    {prepared?.guardian.verdict === "ALLOW" && <section className="panel" aria-labelledby="shop-pay">
      <div className="panel-heading"><h2 id="shop-pay">5. Confirm checkout (PayPal Sandbox)</h2></div>
      {!paid ? <div className="shop-form">
        <label><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} /> I confirm paying{" "}
          {total !== undefined ? formatAmount(total, currency ?? null) : ""} with PayPal Sandbox. The Guardian will decide again before any payment.</label>
        <div><Button disabled={!confirmed || !!busy} onClick={() => void pay()}>{busy === "confirm" ? "Processing… do not refresh" : "Confirm and pay"}</Button></div>
      </div> : paid.outcome === "UNKNOWN" ? <div className="notice amber panel-notice" role="alert"><strong>Outcome unknown</strong><p>{paid.message}</p></div>
        : <dl className="data-grid">
          <div><dt>Outcome</dt><dd><Status value={paid.outcome === "PAID" ? "SUCCESS" : paid.outcome === "FAILED" || paid.outcome === "BLOCKED" ? "FAILED" : paid.outcome} /> {humanize(paid.outcome)}{paid.simulated ? " (SIMULATED)" : ""}</dd></div>
          <div><dt>Guardian</dt><dd>{paid.decision?.verdict} {paid.decision?.reasons.join(", ")}</dd></div>
          <div><dt>PayPal order</dt><dd>{paid.payment?.paypal_order_id ?? "—"}</dd></div>
          <div><dt>PayPal capture</dt><dd>{paid.payment?.paypal_capture_id ?? "—"}</dd></div>
          {paid.payment?.failure_reason && <div><dt>Failure</dt><dd>{paid.payment.failure_reason}</dd></div>}
        </dl>}
    </section>}

    {mandate && paid && paid.outcome !== "UNKNOWN" && <EvidencePanel mandateId={mandate.id} paymentId={paid.payment?.id ?? null}
      simulated={paid.simulated ?? config.data?.engine.simulated ?? false} />}
  </>;
}
