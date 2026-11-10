import type { EvidencePack, Issue } from "@/lib/evidence";

export type Actor = { id: string; role: "viewer" | "reviewer" | "operator" };
export type Action = "submit-evidence" | "reply-inquiry" | "refund" | "accept-claim" | "offer-refund";
export type Strategy = "DEFEND" | "REMEDY" | "INFORMATION";
export const statuses = [
  "PENDING_REVIEW", "ADDITIONAL_INFORMATION_REQUIRED", "READY_FOR_APPROVAL", "APPROVED",
  "SUBMITTING", "SUBMITTED", "CONFIRMED", "FAILED", "UNKNOWN", "REJECTED", "CANCELLED",
] as const;
export type WorkflowStatus = typeof statuses[number];
export const editable: WorkflowStatus[] = [
  "PENDING_REVIEW", "ADDITIONAL_INFORMATION_REQUIRED", "READY_FOR_APPROVAL", "APPROVED", "FAILED",
];
export const actionLabels: Record<Action, string> = {
  "submit-evidence": "Submit evidence", "reply-inquiry": "Reply to inquiry",
  "refund": "Refund payment", "accept-claim": "Accept claim", "offer-refund": "Offer full refund",
};
export type Stats = {
  total: number; awaiting_approval: number; pending_information: number; submitted: number;
  confirmed: number; failed: number; unknown: number; by_status: Record<string, number>;
};
export type WorkflowRow = {
  id: number; dispute_id: number; paypal_dispute_id: string; payment_id: string;
  reason: string | null; amount_minor: number | null; currency: string | null;
  recommendation: string; strategy: Strategy; status: WorkflowStatus;
  assigned_reviewer: string | null; deadline: string | null; updated_at: string;
};
export type Option = { action: Action; amount_minor: number | null; currency: string | null };
export type Plan = {
  option: Option; paypal_status: string; paypal_stage: string | null; capture_id: string | null;
  payload: Record<string, unknown>; evidence_ids: string[]; override_reason: string | null;
};
export type Audit = {
  id: number; revision: number; actor_id: string; event_type: string;
  previous_status: WorkflowStatus | null; new_status: WorkflowStatus;
  details: Record<string, unknown>; created_at: string;
};
export type Workflow = WorkflowRow & {
  revision: number; analysis_id: number; evidence_version: number; stale: boolean; actor: Actor;
  counsel: { executive_summary: string; confidence: number | null;
    reasoning: { id: string; text: string; evidence_ids: string[] }[];
    supporting_evidence_ids: string[]; draft_response: string };
  evidence: { id: string; type: string; source_record_id: string; source_type: string;
    timestamp: string | null; verification_status: string }[];
  verification: EvidencePack["verification_summary"]; gaps: Issue[]; plan: Plan | null; approval: Audit | null;
  requests: { id: string; team: string; description: string; response: string | null;
    requested_at: string; fulfilled_at: string | null }[];
  attempts: { id: number; action: Action; status: string; request_id: string | null;
    response_reference: string | null; created_at: string; finished_at: string | null }[];
  activity: Audit[]; dispute_status: string; paypal_status: string | null;
  paypal_checked_at: string | null; submitted_at: string | null; completed_at: string | null;
};
