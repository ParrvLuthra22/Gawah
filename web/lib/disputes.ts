export const sourceLabels = {
  PAYPAL_API: "PayPal Sandbox",
  MOCK_ENGINE: "Mock engine",
  SIMULATED_FIXTURE: "Simulated fixture",
} as const;

export type Source = keyof typeof sourceLabels;

export const linkLabels = {
  LINKED: "Linked",
  UNMATCHED: "Unmatched",
  AMBIGUOUS: "Ambiguous match",
  LOOKUP_FAILED: "Lookup failed",
} as const;

export type LinkStatus = keyof typeof linkLabels;

export interface Dispute {
  id: number;
  paypal_dispute_id: string;
  transaction_id: string | null;
  payment_id: string | null;
  link_status: LinkStatus;
  reason: string | null;
  status: string;
  amount_minor: number | null;
  currency: string | null;
  created_at: string | null;
  updated_at: string | null;
  deadline: string | null;
  source: Source;
  last_synced_at: string;
}

export interface Party {
  name: string | null;
  email: string | null;
  merchant_id: string | null;
}

export interface DisputeDetail extends Dispute {
  buyer: Party | null;
  merchant: Party | null;
  status_history: {
    previous_status: string | null;
    new_status: string;
    changed_at: string;
    source: Source;
  }[];
}

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface SyncResult {
  id: number;
  started_at: string;
  finished_at: string;
  result: "SUCCESS" | "PARTIAL" | "FAILED";
  retrieved: number;
  created: number;
  updated: number;
  unmatched: number;
  errors: string[];
}

export interface PayPalStatus {
  environment: "sandbox";
  credentials_configured: boolean;
  connection: "CONNECTED" | "DISCONNECTED" | "NOT_CONFIGURED";
  last_sync: SyncResult | null;
  total_disputes: number;
  error: string | null;
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, init);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message ?? "The request failed.");
  return data;
}

export function humanize(code: string | null) {
  return code ? code.toLowerCase().replaceAll("_", " ").replace(/^./, c => c.toUpperCase()) : "—";
}

export function formatDate(value: string | null, withTime = false) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("en", {
    dateStyle: "medium", ...(withTime ? { timeStyle: "short" } : {}), timeZone: "UTC",
  }).format(new Date(value)) + (withTime ? " UTC" : "");
}

export function formatAmount(amount: number | null, currency: string | null) {
  if (amount === null || !currency) return "—";
  const digits = ["JPY", "HUF", "TWD"].includes(currency) ? 0 : 2;
  return new Intl.NumberFormat("en", {
    style: "currency", currency, currencyDisplay: "code",
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  }).format(amount / (10 ** digits));
}
