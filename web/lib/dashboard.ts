import { useQuery } from "@tanstack/react-query";
import { api, type Dispute, type Source } from "@/lib/disputes";

export type Group = { label: string | null; count: number };
export type Amount = { currency: string; amount_minor: number };
export type DailyDisputes = { day: string; count: number; amounts: Amount[] };
export type Overview = {
  total: number; open: number; resolved: number; awaiting_merchant: number;
  due_soon: number; overdue: number; unlinked: number; amounts: Amount[];
  unknown_amounts: number; unknown_open_dates: number;
  resolution_rate: number | null; average_observed_resolution_hours: number | null;
  resolution_samples: number; daily: DailyDisputes[];
  by_status: Group[]; by_reason: Group[]; by_outcome: Group[];
  latest_sync: { result: string; finished_at: string } | null; as_of: string;
};

export type CaseRow = Omit<Dispute, "transaction_id" | "last_synced_at"> & {
  evidence_status: string | null; evidence_version: number | null;
  counsel_status: string | null; counsel_version: number | null;
};
export type CasePage = {
  items: CaseRow[]; total: number; page: number; page_size: number;
  as_of: string; unavailable: string[];
};

export type ArtifactRef = {
  id: number; dispute_id: number | null; paypal_dispute_id: string | null;
  source: Source | null; payment_id: string;
};
export type EvidenceStats = {
  packs_generated: number; latest_packs: number; packs_with_gaps: number;
  missing_records: number; invalid_records: number; contradictions: number;
  verification_rate: number | null; verification: Group[];
  recent: (ArtifactRef & { version: number; generated_at: string; missing: number; invalid: number; contradictions: number })[];
};
export type CounselStats = {
  analyses: number; recommendations: number; pending_reviews: number; drafts: number;
  accepted: number | null; rejected: number | null; by_recommendation: Group[]; by_status: Group[];
  recent: (ArtifactRef & { created_at: string; status: string; review_status: string;
    recommendation: string | null; evidence_version: number })[];
};
export type ActivityFeed = {
  items: (Omit<ArtifactRef, "id" | "payment_id"> & { id: string; timestamp: string;
    payment_id: string | null; type: string; status: string | null; evidence_version: number | null })[];
  unavailable: string[];
};

export const activeStatuses = ["OPEN", "WAITING_FOR_BUYER_RESPONSE", "WAITING_FOR_SELLER_RESPONSE", "UNDER_REVIEW"];

export function useDashboard<T>(section: string, query: string) {
  return useQuery({
    queryKey: ["dashboard", section, query],
    queryFn: ({ signal }) => api<T>(`/dashboard/${section}${query ? `?${query}` : ""}`, { signal }),
  });
}

export function percent(rate: number | null) {
  return rate === null ? "Unavailable" : new Intl.NumberFormat("en", {
    style: "percent", maximumFractionDigits: 1,
  }).format(rate);
}

export function workflowHref(
  section: "evidence" | "counsel", payment: string, dispute: number | null, version?: number | null,
) {
  const query = new URLSearchParams({ payment_id: payment });
  if (dispute !== null) query.set("dispute_id", String(dispute));
  if (version != null) query.set("version", String(version));
  return `/${section}?${query}`;
}
