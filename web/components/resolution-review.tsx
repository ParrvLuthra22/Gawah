"use client";

import { useState } from "react";
import { Panel, Remote, WorkflowBadge } from "@/components/dashboard-panels";
import { Button } from "@/components/ui/button";
import { formatAmount, formatDate, humanize } from "@/lib/disputes";
import { actionLabels, type Action, type Option, type Workflow } from "@/lib/resolution";

export type Change = (path: "review" | "submit" | "reconcile", fields: Record<string, unknown>) => void;
type Options = { options: Option[]; message: string | null };

const area = "block w-full rounded border px-3 py-2";

export function DraftReview({ workflow, options, pending, change }: {
  workflow: Workflow;
  options: { data?: Options; isPending: boolean; isError: boolean; error: Error | null; refetch: () => unknown };
  pending: boolean; change: Change;
}) {
  const edited = workflow.plan && [...workflow.activity].reverse()
    .find(event => event.event_type === "RESPONSE_PREPARED")?.details.merchant_response;
  const [text, setText] = useState(typeof edited === "string" ? edited : workflow.counsel.draft_response);
  const [action, setAction] = useState<Action | "">(workflow.plan?.option.action ?? "");
  const [selected, setSelected] = useState<string[]>(workflow.plan?.evidence_ids ?? []);
  const [overrideReason, setOverrideReason] = useState(workflow.plan?.override_reason ?? "");
  const waiting = workflow.requests.some(request => !request.fulfilled_at);
  return <Panel title="Prepare response">
    <Remote result={options} label="Current Sandbox options">{data => !data.options.length
      ? <p className="notice amber m-4">{data.message ?? "Review unsupported actions in PayPal Sandbox Resolution Center."}</p>
      : <form className="space-y-4 p-5" onSubmit={event => {
        event.preventDefault();
        change("review", { command: "prepare", action, text, evidence_ids: selected, override_reason: overrideReason || null });
      }}>
        <label className="flex flex-col gap-1 text-sm font-semibold">Proposed action
          <select aria-label="Proposed action" required value={action}
            onChange={event => setAction(event.target.value as Action | "")}>
            <option value="">Choose an available action</option>
            {data.options.map(option => <option key={option.action} value={option.action}>
              {actionLabels[option.action]}
              {option.amount_minor !== null && ` · ${formatAmount(option.amount_minor, option.currency)}`}
            </option>)}
          </select>
        </label>
        <label className="block text-sm font-semibold">Merchant response
          <textarea className={area} rows={8} maxLength={2000} value={text} onChange={event => setText(event.target.value)} />
        </label>
        <fieldset className="max-h-60 space-y-2 overflow-auto rounded border p-3">
          <legend className="text-sm font-semibold">Supporting evidence</legend>
          {!workflow.evidence.length && <p className="muted">The pinned pack contains no records.</p>}
          {workflow.evidence.map(item => <label key={item.id} className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={selected.includes(item.id)}
              disabled={item.source_type !== "REAL_RECORD" || item.verification_status === "INVALID"
                || (selected.length >= 12 && !selected.includes(item.id))}
              onChange={event => setSelected(values => event.target.checked ? [...values, item.id]
                : values.filter(id => id !== item.id))} />
            <span>{humanize(item.type)} · {item.source_record_id} · {humanize(item.source_type)}</span>
            <WorkflowBadge status={item.verification_status} />
          </label>)}
        </fieldset>
        <label className="block text-sm font-semibold">Reason for overriding Counsel, if applicable
          <input className={area} maxLength={500} value={overrideReason}
            onChange={event => setOverrideReason(event.target.value)} />
        </label>
        {waiting && <p className="muted">Close the outstanding information requests before preparing a submission.</p>}
        <Button type="submit" disabled={pending || waiting || workflow.stale || !action}>Save and preview package</Button>
      </form>}</Remote>
  </Panel>;
}

export function Approval({ workflow, pending, change, canWrite }: {
  workflow: Workflow; pending: boolean; change: Change; canWrite: boolean;
}) {
  const [confirmation, setConfirmation] = useState("");
  const plan = workflow.plan;
  if (!plan) return null;
  const amount = formatAmount(plan.option.amount_minor, plan.option.currency);
  const consequences: Record<Action, string> = {
    refund: `System 2 issues a full payment refund of ${amount}. PayPal may continue to show the dispute as open.`,
    "accept-claim": `PayPal refunds ${amount} to the buyer and closes the claim.`,
    "offer-refund": `A full-refund offer of ${amount} may immediately refund the buyer and close the inquiry.`,
    "submit-evidence": "PayPal receives the reviewed text and selected record references.",
    "reply-inquiry": "PayPal sends this message to the other party.",
  };
  const canApprove = canWrite && !workflow.stale && workflow.status === "READY_FOR_APPROVAL"
    && (!workflow.assigned_reviewer || workflow.assigned_reviewer === workflow.actor.id);
  return <Panel title="Approval and submission" detail={<WorkflowBadge status={workflow.status} />}>
    <div className="space-y-4 p-5">
      <p className="notice amber">{consequences[plan.option.action]}</p>
      <p className="text-sm"><strong>{actionLabels[plan.option.action]}</strong> · Analysis #{workflow.analysis_id}
        {" · "}Evidence version {workflow.evidence_version}</p>
      <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-slate-50 p-4 text-sm"
        aria-label="Exact submission payload">{JSON.stringify(plan.payload, null, 2)}</pre>
      {workflow.approval && <p className="notice blue">
        Approved by {workflow.approval.actor_id} on {formatDate(workflow.approval.created_at, true)}.
        {workflow.status === "APPROVED" && " Awaiting operator submission."}
      </p>}
      {canApprove && <Button disabled={pending} onClick={() => change("review", { command: "approve" })}>
        Approve this package
      </Button>}
      {canWrite && workflow.status === "READY_FOR_APPROVAL" && workflow.assigned_reviewer
        && workflow.assigned_reviewer !== workflow.actor.id && <p className="muted">
          Approval is assigned to {workflow.assigned_reviewer}.
        </p>}
      {canWrite && !workflow.stale && workflow.actor.role === "operator" && workflow.status === "APPROVED" &&
        <form className="space-y-3" onSubmit={event => {
          event.preventDefault();
          if (confirmation === workflow.paypal_dispute_id) change("submit", { confirmation });
        }}>
          <label className="block text-sm font-semibold">Type {workflow.paypal_dispute_id} to confirm submission
            <input className={area} value={confirmation} autoComplete="off" maxLength={255}
              onChange={event => setConfirmation(event.target.value)} required />
          </label>
          <Button type="submit" disabled={pending || confirmation !== workflow.paypal_dispute_id}>
            {pending ? "Working…" : "Submit approved action"}
          </Button>
        </form>}
    </div>
  </Panel>;
}

export function InformationReview({ workflow, pending, change, canWrite }: {
  workflow: Workflow; pending: boolean; change: Change; canWrite: boolean;
}) {
  const [team, setTeam] = useState("");
  const [request, setRequest] = useState("");
  const [notes, setNotes] = useState("");
  const [response, setResponse] = useState<Record<string, string>>({});
  const [reviewer, setReviewer] = useState(workflow.assigned_reviewer ?? "");
  return <Panel title="Information requests and review notes">
    <div className="space-y-4 p-5">
      <p className="muted">Track requests for internal teams in this workflow.</p>
      {workflow.requests.length === 0 && <p className="muted">No information requests have been recorded.</p>}
      <ul className="dashboard-records">{workflow.requests.map(item => <li key={item.id}>
        <div className="actions"><strong>{item.team}</strong><WorkflowBadge status={item.fulfilled_at ? "FULFILLED" : "OUTSTANDING"} /></div>
        <p>{item.description}</p><p className="muted">{formatDate(item.requested_at, true)}</p>
        {item.fulfilled_at && <p className="mt-2">{item.response} · {formatDate(item.fulfilled_at, true)}</p>}
        {canWrite && !item.fulfilled_at && <form className="actions mt-2" onSubmit={event => {
          event.preventDefault();
          change("review", { command: "receive-info", request_id: item.id, text: response[item.id] });
        }}>
          <input aria-label={`Response from ${item.team}`} placeholder="Information received"
            className="rounded border px-3 py-2" required maxLength={2000} value={response[item.id] ?? ""}
            onChange={event => setResponse(values => ({ ...values, [item.id]: event.target.value }))} />
          <Button variant="outline" type="submit" disabled={pending}>Mark received</Button>
        </form>}
      </li>)}</ul>
      {canWrite && <>
        <form className="space-y-3" onSubmit={event => {
          event.preventDefault(); change("review", { command: "request-info", team, text: request });
        }}>
          <label className="block text-sm font-semibold">Internal team
            <input className={area} value={team} required maxLength={100} onChange={event => setTeam(event.target.value)} />
          </label>
          <label className="block text-sm font-semibold">Information needed
            <textarea className={area} value={request} required maxLength={2000} onChange={event => setRequest(event.target.value)} />
          </label>
          <Button type="submit" variant="outline" disabled={pending}>Record information request</Button>
        </form>
        <div className="space-y-2 border-t pt-4">
          <label className="block text-sm font-semibold">Internal note or decision reason
            <textarea className={area} rows={2} maxLength={2000} value={notes} onChange={event => setNotes(event.target.value)} />
          </label>
          <div className="actions">
            <Button variant="outline" disabled={pending || !notes.trim()} onClick={() => change("review", { command: "note", text: notes })}>Add note</Button>
            <Button variant="outline" disabled={pending || !notes.trim()} onClick={() => change("review", { command: "reject", text: notes })}>Reject recommendation</Button>
            <Button variant="outline" disabled={pending || !notes.trim()} onClick={() => change("review", { command: "cancel", text: notes })}>Cancel workflow</Button>
          </div>
        </div>
        {workflow.actor.role === "operator" && <form className="actions border-t pt-4" onSubmit={event => {
          event.preventDefault(); change("review", { command: "assign", reviewer: reviewer.trim() || null });
        }}>
          <label className="text-sm">Assigned reviewer ID
            <input className="ml-2 rounded border px-3 py-2" maxLength={64} value={reviewer}
              onChange={event => setReviewer(event.target.value)} />
          </label>
          <Button variant="outline" type="submit" disabled={pending}>Save assignment</Button>
        </form>}
      </>}
    </div>
  </Panel>;
}

export function WorkflowHistory({ workflow }: { workflow: Workflow }) {
  return <Panel title="Workflow activity">
    <div className="p-5">
      <h3 className="font-semibold">Submission attempts</h3>
      {workflow.attempts.length === 0 ? <p className="muted">No attempts recorded.</p> :
        <ul className="dashboard-records">{workflow.attempts.map(attempt => <li key={attempt.id}>
          <div className="actions"><strong>{actionLabels[attempt.action]}</strong><WorkflowBadge status={attempt.status} /></div>
          <p className="muted">{formatDate(attempt.created_at, true)} · Request {attempt.request_id}
            {attempt.response_reference && ` · Provider reference ${attempt.response_reference}`}</p>
        </li>)}</ul>}
    </div>
    <ol className="timeline">{workflow.activity.map(event => <li key={event.id}>
      <div className="timeline-change"><strong>
        {event.event_type === "RESPONSE_PREPARED" && Array.isArray(event.details.evidence_reviewed)
          && event.details.evidence_reviewed.length > 0
          ? "Evidence reviewed and response prepared" : humanize(event.event_type)}
      </strong><WorkflowBadge status={event.new_status} /></div>
      <p>{event.actor_id} · {formatDate(event.created_at, true)} · Revision {event.revision}</p>
      {!!Object.keys(event.details).length && <details className="mt-2">
        <summary className="record-link cursor-pointer">View audit details</summary>
        <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-slate-50 p-3">
          {JSON.stringify(event.details, null, 2)}
        </pre>
      </details>}
    </li>)}</ol>
  </Panel>;
}
