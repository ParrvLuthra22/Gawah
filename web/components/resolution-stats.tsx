"use client";

import { Breakdown } from "@/components/dashboard-analytics";
import type { Stats } from "@/lib/resolution";

export function ResolutionStats({ data }: { data: Stats }) {
  const metrics = [
    ["Total workflows", data.total], ["Awaiting approval", data.awaiting_approval],
    ["Pending information", data.pending_information], ["Actions submitted", data.submitted],
    ["Confirmed", data.confirmed], ["Failed submissions", data.failed],
    ["Outcome uncertain", data.unknown],
  ] as const;
  return <div className="space-y-4 p-5">
    <dl className="dashboard-facts">
      {metrics.map(([label, value]) => <div key={label}>
        <dt>{label}</dt><dd>{value.toLocaleString("en")}</dd>
      </div>)}
    </dl>
    <Breakdown title="Workflow status" groups={Object.entries(data.by_status).map(([label, count]) => ({ label, count }))} />
  </div>;
}
