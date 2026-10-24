"use client";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { Badge, Err, Money, Shell, Stat, statusTone, useAction, useLoad } from "@/components/ui";
import { api } from "@/lib/api";
import { useSession } from "@/lib/session";

const COLS: [string, string][] = [
  ["agent_commission", "Agent pay"], ["manager_override", "Overrides"], ["gross_compensation", "Gross"],
  ["chargeback_recovery", "CB recovered"], ["ending_chargeback", "CB carry"], ["manual_hold", "Held"],
  ["room_deduction", "Room"], ["advance_deduction", "Advance"], ["flight_deduction", "Flight"], ["other_deduction", "Other"],
  ["cash_credits", "Credits"], ["final_check", "Check"], ["advance_ending", "Adv. balance"],
];

export default function Run() {
  const { run } = useParams<{ run: string }>();
  const { data, error, reload } = useLoad<any>(`/runs/${run}`);
  const [q, setQ] = useState("");
  const [onlyPay, setOnlyPay] = useState(false);
  const act = useAction();
  const router = useRouter();
  const { can } = useSession();
  const { data: cmp } = useLoad<any[]>(data?.supersedes ? `/runs/${data.supersedes}/compare/${run}` : null, [data?.supersedes]);
  const people = useMemo(() => (data?.people || []).filter((p: any) =>
    p.payee.toLowerCase().includes(q.toLowerCase()) && (!onlyPay || Number(p.final_check) > 0 || Number(p.data.gross_compensation) > 0)), [data, q, onlyPay]);
  if (!data) return <Shell title="Payroll run"><Err error={error} /></Shell>;
  const t = data.totals || {};
  const blocking = (data.review || []).filter((r: any) => r.blocking && r.status === "open");
  const finalize = () => {
    if (!confirm(`Finalize ${data.week} revision ${data.revision}? Checks total ${t.total_checks}. This posts ledgers and creates calculated payments.`)) return;
    const key = `${data.id}:${crypto.randomUUID()}`;
    act.run(async () => { await api(`/runs/${run}/finalize`, { method: "POST", json: { finalize_key: key } }); reload(); });
  };
  const sum = (k: string) => people.reduce((a: number, p: any) => a + Math.round(Number(p.data[k] || 0) * 100), 0) / 100;
  return (
    <Shell title={`${data.week} · revision ${data.revision}`} actions={<>
      <Badge tone={statusTone(data.status)}>{data.status}</Badge>
      {data.status === "calculated" && can("payroll.finalize") && <button className="primary" disabled={act.busy || blocking.length > 0} onClick={finalize}>{data.supersedes ? "Finalize correction" : "Finalize week"}</button>}
      <Link className="btn" href={`/reports?run=${run}`}>Reports</Link>
    </>}>
      <Err error={act.error} />
      {act.items?.length ? <div className="alert error">{act.items.map((i: any) => <div key={i.id}>{i.kind}: {i.message}</div>)}</div> : null}
      {data.correction_reason && <div className="alert warn">Correction of a finalized week: {data.correction_reason}</div>}
      {blocking.length > 0 && <div className="alert error">{blocking.length} blocking review item(s) must be resolved before finalizing. <Link href={`/review?week=${data.week}`}>Open review queue</Link></div>}
      <div className="grid">
        <Stat label="Checks" value={t.total_checks} tone="ok" />
        <Stat label="Payable checks" value={t.positive_checks} money={false} />
        <Stat label="Gross" value={t.gross_compensation} />
        <Stat label="Chargebacks recovered" value={t.chargeback_recovery} />
        <Stat label="Held" value={t.manual_holds} />
        <Stat label="Room deductions" value={t.room_deductions} />
        <Stat label="Advance deductions" value={t.advance_deductions} />
        <Stat label="Flight deductions" value={t.flight_deductions} />
        {Object.entries(t.company_payables || {}).map(([k, v]) => <Stat key={k} label={`${k} (excluded from payroll)`} value={v} />)}
        <Stat label="Non-payable rows" value={t.nonpayable_rows} money={false} />
      </div>
      {cmp && (
        <div className="panel"><h3 style={{ marginTop: 0 }}>Changes against the revision this correction replaces</h3>
          {cmp.length === 0 ? <span className="muted">No per-person differences.</span> : cmp.map((c) => (
            <div key={c.payee}><strong>{c.payee}</strong>: {Object.entries(c.diffs).map(([k, d]: any) => <span key={k} className="small"> {k} <span className="diff-del">{d.before}</span> → <span className="diff-add">{d.after}</span>;</span>)}</div>
          ))}
        </div>
      )}
      <div className="row" style={{ marginBottom: 8 }}>
        <input placeholder="Search person" value={q} onChange={(e) => setQ(e.target.value)} />
        <label className="row small"><input type="checkbox" checked={onlyPay} onChange={(e) => setOnlyPay(e.target.checked)} /> With pay only</label>
        <Link href={`/review?week=${data.week}`}>{(data.review || []).filter((r: any) => r.status === "open").length} open review items</Link>
      </div>
      <div className="tablewrap"><table>
        <thead><tr><th>Person</th><th>Status</th>{COLS.map(([k, l]) => <th key={k} className="r">{l}</th>)}</tr></thead>
        <tbody>{people.map((p: any) => (
          <tr key={p.payee} className="click" onClick={() => router.push(`/payroll/${run}/${encodeURIComponent(p.payee)}`)}>
            <td>{p.payee}</td><td><Badge tone={statusTone(p.status)}>{p.status}</Badge></td>
            {COLS.map(([k]) => <td key={k} className="r">{Number(p.data[k]) ? <Money v={p.data[k]} strong={k === "final_check"} /> : <span className="muted">–</span>}</td>)}
          </tr>
        ))}</tbody>
        <tfoot><tr><td>{people.length} people</td><td></td>{COLS.map(([k]) => <td key={k} className="r num">{k === "advance_ending" || k === "ending_chargeback" ? "" : sum(k).toFixed(2)}</td>)}</tr></tfoot>
      </table></div>
    </Shell>
  );
}
