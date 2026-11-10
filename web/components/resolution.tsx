import Link from "next/link";
import { Panel } from "@/components/dashboard-panels";
import { Button } from "@/components/ui/button";
import type { DisputeDetail } from "@/lib/disputes";

export function Resolution({ dispute }: { dispute: DisputeDetail }) {
  return <Panel title="Sandbox resolution">
    <div className="p-5">
      {dispute.source !== "PAYPAL_API" ? <p>Simulated disputes are read-only.</p>
        : <Button asChild variant="outline">
          <Link href={`/resolution?dispute_id=${dispute.id}`}>Open Resolution Center</Link>
        </Button>}
    </div>
  </Panel>;
}
