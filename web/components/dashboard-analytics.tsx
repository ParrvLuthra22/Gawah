"use client";

import { useState } from "react";
import { Empty } from "@/components/dispute-states";
import { percent, type Group, type Overview } from "@/lib/dashboard";
import { formatAmount, formatDate, humanize } from "@/lib/disputes";

type Point = { day: string; value: number };

export function Breakdown({ title, groups, unknown = "Unavailable" }: {
  title: string; groups: Group[]; unknown?: string;
}) {
  const max = groups.reduce((largest, group) => Math.max(largest, group.count), 1);
  return <div className="min-w-0">
    <h3 className="mb-2 text-sm font-semibold">{title}</h3>
    {groups.length === 0 ? <p className="muted">No records in this view.</p> :
      <ul className="space-y-2">{groups.map(group => <li key={group.label ?? ""}>
        <div className="flex justify-between gap-3 text-sm">
          <span className="break-words">{group.label ? humanize(group.label) : unknown}</span>
          <span className="font-semibold tabular-nums">{group.count.toLocaleString("en")}</span>
        </div>
        <div className="h-1.5 rounded-full bg-blue-50" aria-hidden="true">
          <div className="h-full rounded-full bg-[#3479bf]" style={{ width: `${100 * group.count / max}%` }} />
        </div>
      </li>)}</ul>}
  </div>;
}

function TimeChart({ title, points, format }: {
  title: string; points: Point[]; format: (value: number) => string;
}) {
  if (!points.length) return <div>
    <h3 className="text-sm font-semibold">{title}</h3><p className="muted mt-2">No dated records in this view.</p>
  </div>;
  const first = points[0].day;
  const last = points[points.length - 1].day;
  const days = (day: string) => (Date.parse(day) - Date.parse(first)) / 86_400_000;
  const width = Math.min(30, Math.max(1, 395 / (days(last) + 1) * .7));
  const max = points.reduce((largest, point) => Math.max(largest, point.value), 0);
  const start = 6 + width / 2;
  const span = 408 - width;
  return <figure className="min-w-0">
    <figcaption className="mb-1 text-sm font-semibold">{title}</figcaption>
    <div className="muted flex justify-between">
      <span>Peak {format(max)}</span><span>UTC</span>
    </div>
    <svg viewBox="0 0 420 126" className="mt-2 w-full rounded-md bg-[#f6faff]" role="img"
      aria-label={`${title} from ${formatDate(first)} to ${formatDate(last)}. Exact values are below.`}>
      {[12, 62, 112].map(y => <line key={y} x1="6" x2="414" y1={y} y2={y} stroke="#dfe7f0" />)}
      {points.map(point => {
        const height = max ? point.value / max * 100 : 0;
        const x = first === last ? 210 : start + (days(point.day) / days(last)) * span;
        return <rect key={point.day} x={x - width / 2} y={112 - height} width={width}
          height={height} rx={Math.min(2, width / 3)} fill="#2369b2">
          <title>{formatDate(point.day)} · {format(point.value)}</title>
        </rect>;
      })}
    </svg>
    <div className="muted mt-1 flex justify-between"><time dateTime={first}>{formatDate(first)}</time>
      {first !== last && <time dateTime={last}>{formatDate(last)}</time>}</div>
    <details className="mt-2 text-sm">
      <summary className="record-link cursor-pointer">View daily values</summary>
      <div className="max-h-52 overflow-auto" tabIndex={0} role="region" aria-label={`${title} values`}>
        <table className="history-table"><thead><tr><th scope="col">UTC date</th><th scope="col">Value</th></tr></thead>
          <tbody>{points.map(point => <tr key={point.day}>
            <td>{formatDate(point.day)}</td><td>{format(point.value)}</td>
          </tr>)}</tbody>
        </table>
      </div>
    </details>
  </figure>;
}

export function DashboardAnalytics({ data }: { data: Overview }) {
  const [selected, setSelected] = useState("");
  const currencies = data.amounts.map(amount => amount.currency);
  const currency = currencies.includes(selected) ? selected : currencies[0];
  const datedAmounts = data.daily.flatMap(day => {
    const amount = day.amounts.find(amount => amount.currency === currency);
    return amount ? [{ day: day.day, value: amount.amount_minor }] : [];
  });
  if (!data.total) return <Empty title="No disputes in this period"
    message="Charts appear after a dispute is recorded in the selected view." />;
  return <div className="space-y-5 p-5">
    <dl className="dashboard-facts">
      <div><dt>Resolution rate</dt><dd>{percent(data.resolution_rate)}</dd></div>
      <div><dt>Average time to observed resolution</dt><dd>
        {data.average_observed_resolution_hours === null
          ? "Unavailable" : `${data.average_observed_resolution_hours.toLocaleString("en")} h`}
        <span className="muted block font-normal">{data.resolution_samples} recorded status transitions</span>
      </dd></div>
    </dl>
    <div className="dashboard-columns border-y border-[#dfe7f0] py-4">
      <TimeChart title="Disputes opened over time" points={data.daily.map(day => ({ day: day.day, value: day.count }))}
        format={value => value.toLocaleString("en")} />
      <div className="min-w-0">
        {currencies.length > 1 && <label className="muted mb-2 flex items-center gap-2">Amount currency
          <select aria-label="Amount currency" value={currency} onChange={event => setSelected(event.target.value)}>
            {currencies.map(code => <option key={code}>{code}</option>)}
          </select>
        </label>}
        <TimeChart title="Recorded disputed amounts over time" points={datedAmounts}
          format={value => formatAmount(value, currency ?? null)} />
      </div>
    </div>
    {!!data.unknown_open_dates && <p className="muted">{data.unknown_open_dates} cases have no opening timestamp and are omitted from time charts.</p>}
    <div className="dashboard-columns dashboard-three">
      <Breakdown title="Dispute status" groups={data.by_status} />
      <Breakdown title="Dispute reason" groups={data.by_reason} />
      <Breakdown title="Reported outcomes for resolved cases" groups={data.by_outcome} />
    </div>
  </div>;
}
