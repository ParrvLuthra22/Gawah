export type VerificationStatus = "VERIFIED" | "INVALID" | "UNVERIFIED" | "PENDING" | "UNAVAILABLE";

export const evidenceSourceTypes = {
  REAL_RECORD: "Real record",
  MOCK_RESPONSE: "Mock response",
  SIMULATED_FIXTURE: "Simulated fixture",
  UNKNOWN: "Source unconfirmed",
} as const;

export type SourceType = keyof typeof evidenceSourceTypes;

export const evidenceSources = {
  SYSTEM_1: "System 1",
  SYSTEM_2: "System 2",
  SYSTEM_3: "System 3 · Disputes",
  MOCK_ENGINE: "Mock engine",
} as const;

type Source = keyof typeof evidenceSources;

export interface EvidenceItem {
  id: string;
  type: string;
  source_system: Source;
  source_type?: SourceType;
  source_record_id: string;
  source_reference: string;
  timestamp: string | null;
  verification_status: VerificationStatus;
  verification?: Verification | null;
}

export interface TimelineEvent {
  id: string;
  type: string;
  timestamp: string | null;
  source_system: Source;
  source_type?: SourceType;
  record_id: string;
  description: string;
  verification_status: VerificationStatus;
}

export interface Issue {
  code: string;
  type: "MISSING" | "INVALID" | "CONTRADICTION";
  severity: "INFO" | "WARNING" | "HIGH";
  explanation: string;
  evidence_ids: string[];
  source_references: string[];
}

interface Verification {
  status: VerificationStatus;
  reference: string | null;
  detail: string;
  result: { reason?: string | null } | null;
}

export interface EvidencePack {
  id: number;
  version: number;
  generated_at: string;
  payment_id: string;
  dispute_id: string | null;
  dispute_record_id?: number | null;
  evidence: EvidenceItem[];
  timeline: TimelineEvent[];
  verification_summary: Record<"mandate_signature" | "cart_signature" | "ledger" | "engine_ledger", Verification> & {
    payment_reference_match?: Verification | null;
  };
  sources: {
    source_system: Source;
    reference: string;
    status: "AVAILABLE" | "NOT_FOUND" | "UNAVAILABLE" | "INVALID";
    detail: string | null;
  }[];
  missing_evidence: Issue[];
  invalid_evidence: Issue[];
  contradictions: Issue[];
  completeness: { available: number; missing: number; verified: number; unverified: number };
}

export async function loadEvidence(path: string, signal: AbortSignal): Promise<EvidencePack | null> {
  const response = await fetch(`/api${path}`, { signal });
  const data = await response.json();
  if (response.status === 404 && data.error?.code === "EVIDENCE_PACK_NOT_FOUND") return null;
  if (!response.ok) throw new Error(data.error?.message ?? "Evidence could not be retrieved.");
  return data;
}
