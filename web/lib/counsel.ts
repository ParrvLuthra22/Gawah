import type { Issue, SourceType, VerificationStatus } from "@/lib/evidence";

export const recommendations = {
  DEFEND: "Defend",
  CONCEDE_REMEDY: "Concede or remedy",
  REQUEST_MORE_EVIDENCE: "Request more evidence",
} as const;

export interface CounselAnalysis {
  id: number;
  payment_id: string;
  dispute_id: string | null;
  dispute_record_id: number | null;
  category: string;
  evidence_pack_version: number;
  analysis_status: "COMPLETED" | "REVIEW_REQUIRED" | "FAILED";
  review_status: "PENDING";
  human_review_required: true;
  model: string;
  created_at: string;
  error: { code: string; message: string } | null;
  classification: {
    records: { id: string; source_type: SourceType; verification_status: VerificationStatus }[];
    contradictory_evidence: Issue[];
    missing_evidence: Issue[];
    invalid_evidence: Issue[];
    invalid_evidence_ids: string[];
    unverified_evidence_ids: string[];
    unavailable_evidence_ids: string[];
    unavailable_sources: { reference: string; detail: string | null }[];
    verification_warnings: string[];
  };
  outcome: {
    recommendation: keyof typeof recommendations;
    confidence: number | null;
    conflict: string | null;
    executive_summary: string;
    reasoning: { id: string; text: string; evidence_ids: string[] }[];
    supporting_evidence_ids: string[];
    draft_response: string;
    draft_evidence_ids: string[];
  } | null;
}

export function evidenceHref(payment: string, dispute?: number | string | null, version?: number, ref?: string) {
  const query = new URLSearchParams({ payment_id: payment });
  if (dispute) query.set("dispute_id", String(dispute));
  if (version) query.set("version", String(version));
  return `/evidence?${query}${ref ? `#${encodeURIComponent(ref)}` : ""}`;
}

export async function loadCounsel(path: string, signal: AbortSignal): Promise<CounselAnalysis | null> {
  const response = await fetch(`/api${path}`, { signal });
  const data = await response.json();
  if (response.status === 404 && data.error?.code === "COUNSEL_ANALYSIS_NOT_FOUND") return null;
  if (!response.ok) throw new Error(data.error?.message ?? "Counsel analysis could not be retrieved.");
  return data;
}
