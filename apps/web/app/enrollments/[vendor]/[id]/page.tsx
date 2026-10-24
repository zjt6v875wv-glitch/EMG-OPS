"use client";
import { useParams } from "next/navigation";
import { Badge, Err, Json, Money, Shell, useLoad } from "@/components/ui";

export default function Enrollment() {
  const { vendor, id } = useParams<{ vendor: string; id: string }>();
  const { data, error } = useLoad<any>(`/enrollments/${vendor}/${id}`);
  const e = data?.enrollment;
  return (
    <Shell title={`${vendor} · ${decodeURIComponent(id)}`}>
      <Err error={error} />
      {e && <>
        <div className="panel"><dl className="kv">
          <dt>Customer</dt><dd>{e.customer_name} {e.customer_id && <span className="muted">({e.customer_id})</span>}</dd>
          <dt>Rep on file</dt><dd>{e.raw_rep}</dd><dt>Utility / product</dt><dd>{e.utility} · {e.product} · {e.premise}</dd>
          <dt>kWh</dt><dd>{e.kwh}</dd><dt>Status</dt><dd>{e.status} {e.status_reason && <span className="muted">· {e.status_reason}</span>}</dd>
          <dt>Sale date</dt><dd>{e.sale_date}</dd><dt>Approval date</dt><dd>{e.approval_date} {e.approval_date_source && <Badge>{e.approval_date_source}</Badge>}</dd><dt>First invoice</dt><dd>{e.invoice_date}</dd>
          <dt>Address</dt><dd>{[e.service_address, e.city, e.state, e.zip].filter(Boolean).join(", ")}</dd>
        </dl></div>
        <h2>Status history</h2>
        <div className="tablewrap"><table><thead><tr><th>Observed</th><th>Status</th><th className="wrap">Reason</th><th>Source file</th><th>Row</th></tr></thead>
          <tbody>{data.status_history.map((h: any) => <tr key={h.id}><td>{new Date(h.observed_at).toLocaleString()}</td><td>{h.status}</td><td className="wrap">{h.reason}</td><td>{h.source_file}</td><td>{h.source_row}</td></tr>)}</tbody></table></div>
        <h2>Vendor money lines</h2>
        <div className="tablewrap"><table><thead><tr><th>Type</th><th>Invoice</th><th>Date</th><th className="r">Amount</th><th>Source</th></tr></thead>
          <tbody>{data.invoice_lines.map((l: any) => <tr key={l.id}><td>{l.line_type}</td><td>{l.invoice_number}</td><td>{l.invoice_date}</td><td className="r"><Money v={l.amount} /></td><td className="small">{l.source_file} row {l.source_row} · <a href={`/imports/${l.batch_id}`}>batch</a></td></tr>)}</tbody></table></div>
        <h2>Payouts</h2>
        <div className="tablewrap"><table><thead><tr><th>Week</th><th>Payee</th><th>Activity</th><th className="r">Rate</th><th className="r">Amount</th><th>Rules</th><th>Source</th><th>Active</th></tr></thead>
          <tbody>{data.payouts.map((p: any) => <tr key={p.id}><td>{p.week}</td><td>{p.payee}</td><td>{p.activity}</td><td className="r num">{p.rate}</td><td className="r"><Money v={Number(p.amount).toFixed(2)} /></td><td>{p.rule_set_version ? `v${p.rule_set_version}` : ""}</td><td className="small">{p.source} {p.proof}</td><td>{p.active ? "yes" : <Badge>retired</Badge>}</td></tr>)}</tbody></table></div>
      </>}
    </Shell>
  );
}
