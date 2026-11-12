import { CheckCircle2, ShieldAlert, Star } from "lucide-react";
import { Button } from "@/components/ui/button";
import { humanize } from "@/lib/disputes";
import type { Recommendation } from "@/lib/shop";

export function ProductCard({ rec, best, selected, onSelect }: {
  rec: Recommendation; best?: boolean; selected?: boolean; onSelect: (id: string) => void;
}) {
  const { product, checkout } = rec;
  const verified = rec.verification_status === "eligible";
  return <article className="shop-card" data-best={best || undefined} aria-label={product.title}>
    <div className="shop-card-top">
      {best && <span className="status-pill green"><Star size={12} aria-hidden="true" /> Best match</span>}
      <span className={`status-pill ${verified ? "green" : "amber"}`}>{humanize(rec.verification_status)}</span>
      <span className={`status-pill ${checkout.eligible ? "blue" : "red"}`}>
        {checkout.eligible ? "Eligible for checkout" : "Not eligible for checkout"}
      </span>
    </div>
    <h3>{product.title}</h3>
    <p className="muted">{product.brand} · {product.merchant_id}</p>
    <p className="shop-price">{product.currency} {product.price}</p>
    <dl className="shop-attrs">
      {Object.entries(product.attributes).slice(0, 6).map(([k, v]) => <div key={k}><dt>{humanize(k)}</dt><dd>{v}</dd></div>)}
    </dl>
    <p>{rec.rank_reason}</p>
    {!!rec.missing_verifications.length
      && <p className="muted"><ShieldAlert size={14} aria-hidden="true" /> Unverified: {rec.missing_verifications.join(", ")}</p>}
    <p className="muted">{checkout.reason}</p>
    <Button disabled={!checkout.eligible} aria-pressed={selected} onClick={() => onSelect(product.id)}>
      {selected ? <><CheckCircle2 size={16} aria-hidden="true" /> Selected</> : "Select this product"}
    </Button>
  </article>;
}
