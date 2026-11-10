"use client";

import type { ReactNode } from "react";
import { Failed } from "@/components/dispute-states";
import { humanize } from "@/lib/disputes";

export function Panel({ title, id, detail, children }: {
  title: string; id?: string; detail?: ReactNode; children: ReactNode;
}) {
  return <section id={id} className="panel" aria-label={title}>
    <div className="panel-heading flex-wrap justify-between"><h2>{title}</h2>{detail}</div>
    {children}
  </section>;
}

export function Skeleton({ label }: { label: string }) {
  return <div role="status" className="space-y-3 p-5">
    <span className="sr-only">{label}</span>
    {[100, 80, 90].map(width => <div key={width} aria-hidden="true"
      className="h-4 animate-pulse rounded bg-blue-100 motion-reduce:animate-none"
      style={{ width: `${width}%` }} />)}
  </div>;
}

export function Remote<T>({ result, label, children }: {
  result: { data?: T; isPending: boolean; isError: boolean; error: Error | null; refetch: () => unknown };
  label: string; children: (data: T) => ReactNode;
}) {
  if (result.isPending) return <Skeleton label={`Loading ${label}…`} />;
  if (result.data === undefined) return <Failed error={new Error(`${label} is unavailable. ${result.error?.message ?? ""}`)}
    retry={() => void result.refetch()} />;
  return <>
    {result.isError && <p className="notice amber m-4" role="status">
      Showing cached {label.toLowerCase()} after a refresh failed.{" "}
      <button type="button" className="record-link" onClick={() => void result.refetch()}>Retry refresh</button>
    </p>}
    {children(result.data)}
  </>;
}

export function WorkflowBadge({ status }: { status: string | null }) {
  const colors: Record<string, string> = {
    SAVED: "green", COMPLETED: "green", SUCCEEDED: "green", VERIFIED: "green",
    FAILED: "red", INVALID: "red", INVALID_CHECK_PRESENT: "red",
    SAVED_WITH_GAPS: "amber", STALE: "amber", PENDING: "amber", PENDING_HUMAN_REVIEW: "amber",
    REVIEW_REQUIRED: "amber", UNKNOWN: "amber", NOT_ASSEMBLED: "amber", NOT_ANALYZED: "amber",
  };
  const label = status ? (/^[A-Z_]+$/.test(status) ? humanize(status) : status) : "Unavailable";
  return <span className={`status-pill ${status ? colors[status] ?? "blue" : "amber"}`}>{label}</span>;
}
