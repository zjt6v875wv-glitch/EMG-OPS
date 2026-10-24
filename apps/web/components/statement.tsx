"use client";
import { Badge, Money, statusTone } from "@/components/ui";

const SUMMARY: [string, string][] = [
  ["agent_commission", "Agent commission"], ["manager_override", "Manager overrides / top-ups"], ["gross_compensation", "Gross compensation"],
  ["opening_chargeback", "Opening chargeback balance"], ["new_chargebacks", "New verified chargebacks"], ["chargeback_recovery", "Chargeback recovery"],
  ["ending_chargeback", "Chargeback carry-forward"], ["manual_hold", "Held (not applied to any debt)"], ["reserve_withheld", "Clawback reserve"],
  ["room_opening", "Room opening"], ["room_new", "New room charges"], ["room_deduction", "Room deduction"], ["room_ending", "Room carry-forward"],
  ["advance_opening", "Advance opening"], ["new_advance", "New advances"], ["advance_deduction", "Advance deduction"], ["advance_ending", "Advance balance"],
  ["flight_opening", "Flight opening"], ["flight_new", "New flight charges"], ["flight_deduction", "Flight deduction"], ["flight_ending", "Flight carry-forward"],
  ["other_opening", "Other opening"], ["other_new", "Other new"], ["other_deduction", "Other deduction"], ["other_ending", "Other carry-forward"],
  ["protected_check", "Protected minimum check"], ["commission_payment", "Commission payment"], ["cash_credits", "Refunds / reimbursements / bonuses"],
];

export function Statement({ d }: { d: any }) {
  const r = d.result;
  return (
    <>
      <div className="grid">
        <div className="stat ok"><div className="muted small">Final check</div><div className="big"><Money v={r.final_check} /></div><Badge tone={statusTone(r.status)}>{r.status}</Badge></div>
        <div className="stat"><div className="muted small">Gross</div><div className="big"><Money v={r.gross_compensation} /></div></div>
        <div className="stat"><div className="muted small">Advance balance</div><div className="big"><Money v={r.advance_ending} /></div></div>
        <div className="stat"><div className="muted small">Chargeback carry</div><div className="big"><Money v={r.ending_chargeback} /></div></div>
      </div>
      <div className="panel">
        <dl className="kv">{SUMMARY.filter(([k]) => Number(r[k])).map(([k, l]) => <><dt key={k}>{l}</dt><dd key={k + "v"}><Money v={r[k]} /></dd></>)}</dl>
        {Object.keys(r.advance_by_creditor || {}).length > 0 && <>
          <h3>Advances by creditor</h3>
          <dl className="kv">{Object.entries(r.advance_by_creditor).map(([c, v]: any) => <><dt key={c}>{c}</dt><dd key={c + "v"}>owed {v.owed} · deducted {v.deducted} · balance {v.ending}</dd></>)}</dl>
        </>}
        {Object.values(r.collection_basis || {}).some(Boolean) && <><h3>Collection basis</h3>
          <dl className="kv">{Object.entries(r.collection_basis).filter(([, v]) => v).map(([k, v]: any) => <><dt key={k}>{k}</dt><dd key={k + "v"}>{v}</dd></>)}</dl></>}
        {r.notes?.length > 0 && <><h3>Notes</h3><ul>{r.notes.map((n: string, i: number) => <li key={i}>{n}</li>)}</ul></>}
      </div>
      <h2>Earnings detail ({d.lines.length})</h2>
      <div className="tablewrap"><table>
        <thead><tr><th>Activity</th><th>Vendor</th><th>Rep</th><th>Enrollment</th><th>Customer</th><th>Utility</th><th>Product</th><th className="r">kWh / units</th><th className="r">Rate</th><th className="r">Amount</th><th>Sale date</th><th>Rule</th><th className="wrap">Notes</th></tr></thead>
        <tbody>{d.lines.map((l: any) => (
          <tr key={l.id}><td>{l.activity}</td><td>{l.vendor}</td><td>{l.rep}</td><td><a href={`/enrollments/${l.vendor}/${encodeURIComponent(l.enrollment_id)}`}>{l.enrollment_id}</a></td><td>{l.customer}</td><td>{l.utility}</td><td>{l.product}</td>
            <td className="r num">{Number(l.kwh) ? l.kwh : l.units}</td><td className="r num">{String(Number(l.rate))}</td><td className="r"><Money v={Number(l.amount).toFixed(2)} /></td><td>{l.sale_date}</td><td className="small muted">{l.rule_id}</td><td className="wrap small">{l.notes}</td></tr>
        ))}</tbody>
      </table></div>
      {d.chargebacks.length > 0 && <><h2>Chargebacks</h2>
        <div className="tablewrap"><table>
          <thead><tr><th>Vendor</th><th>Enrollment</th><th>Rep</th><th>Customer</th><th className="r">Amount</th><th>Verified</th><th>Original week</th><th className="wrap">Evidence</th></tr></thead>
          <tbody>{d.chargebacks.map((c: any) => <tr key={c.id}><td>{c.vendor}</td><td>{c.enrollment_id}</td><td>{c.data.rep}</td><td>{c.data.customer}</td><td className="r"><Money v={c.amount} /></td>
            <td>{c.verified ? <Badge tone="ok">verified</Badge> : <Badge tone="warn">held</Badge>}</td><td>{c.data.original_week}</td><td className="wrap small">{c.data.evidence}</td></tr>)}</tbody>
        </table></div></>}
      {d.nonpayable.length > 0 && <><h2>Non-payable and pending ({d.nonpayable.length})</h2>
        <div className="tablewrap"><table>
          <thead><tr><th>Kind</th><th>Vendor</th><th>Enrollment</th><th>Customer</th><th>Sale date</th><th className="wrap">Status</th></tr></thead>
          <tbody>{d.nonpayable.map((n: any) => <tr key={n.id}><td>{n.kind}</td><td>{n.vendor}</td><td>{n.enrollment_id}</td><td>{n.data.customer}</td><td>{n.data.sale_date}</td><td className="wrap small">{n.data.status}</td></tr>)}</tbody>
        </table></div></>}
    </>
  );
}
