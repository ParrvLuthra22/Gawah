"use client";

import { AlertCircle, Inbox, LoaderCircle, RefreshCw } from "lucide-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, humanize, type SyncResult } from "@/lib/disputes";
import { Button } from "@/components/ui/button";

export function Pending({ message }: { message: string }) {
  return <div className="panel-state" role="status"><LoaderCircle className="animate-spin" />{message}</div>;
}

export function Failed({ error, retry }: { error: Error; retry?: () => void }) {
  return (
    <div className="panel-state" role="alert">
      <AlertCircle className="text-red-600" />
      <p>{error.message}</p>
      {retry && <Button variant="outline" onClick={retry}>Try again</Button>}
    </div>
  );
}

export function Empty({ title, message }: { title: string; message: string }) {
  return (
    <div className="panel-state">
      <span className="empty-icon"><Inbox size={22} aria-hidden="true" /></span>
      <strong>{title}</strong>
      <p>{message}</p>
    </div>
  );
}

export function Status({ value }: { value: string }) {
  const tone = ["RESOLVED", "SUCCESS", "CONNECTED"].includes(value) ? "green"
    : ["FAILED", "DISCONNECTED"].includes(value) ? "red"
    : ["PARTIAL", "NOT_CONFIGURED"].includes(value) || value.startsWith("WAITING") ? "amber"
    : "blue";
  return <span className={`status-pill ${tone}`}>{humanize(value)}</span>;
}

export function useSync() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api<SyncResult>("/disputes/sync", { method: "POST" }),
    onSettled: () => Promise.all([
      queryClient.invalidateQueries({ queryKey: ["disputes"] }),
      queryClient.invalidateQueries({ queryKey: ["paypal"] }),
      queryClient.invalidateQueries({ queryKey: ["dashboard"] }),
    ]),
  });
}

export function SyncButton({ sync }: { sync: ReturnType<typeof useSync> }) {
  return (
    <Button onClick={() => sync.mutate()} disabled={sync.isPending}>
      <RefreshCw size={16} aria-hidden="true" className={sync.isPending ? "animate-spin" : ""} />
      {sync.isPending ? "Syncing disputes…" : "Sync disputes"}
    </Button>
  );
}

export function SyncFeedback({ sync }: { sync: ReturnType<typeof useSync> }) {
  if (sync.isError) {
    return <div className="notice red" role="alert"><strong>Sync failed.</strong> {sync.error.message}</div>;
  }
  if (!sync.data) return null;
  const result = sync.data;
  const title = {
    SUCCESS: "Sync complete.",
    PARTIAL: "Sync partially completed.",
    FAILED: "Sync failed.",
  }[result.result];
  const color = { SUCCESS: "green", PARTIAL: "amber", FAILED: "red" }[result.result];
  return (
    <div className={`notice ${color}`} role={result.result === "FAILED" ? "alert" : "status"}>
      <strong>{title}</strong> {result.retrieved} retrieved, {result.created} new, {result.updated} updated, {result.unmatched} without a payment link.
      {result.errors.map(error => <p key={error}>{error}</p>)}
    </div>
  );
}
