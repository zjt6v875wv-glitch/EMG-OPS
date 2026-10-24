"use client";
import Link from "next/link";
import { useState } from "react";
import { Badge, Err, Money, Shell, Stat, statusTone, useLoad } from "@/components/ui";
import { useSession } from "@/lib/session";

export default function Dashboard() {
  const [week, setWeek] = useState<string | null>(null);
  const { data, error } = useLoad<any>(`/dashboard${week ? `?week=${week}` : ""}`, [week]);
  const { can } = useSession();
  const t = data?.run?.totals || {};
  return (
    <Shell title="Dashboard" actions={
      data?.weeks?.length ? (
        <select value={data.week || ""} onChange={(e) => setWeek(e.target.value)}>
          {data.weeks.map((w: any) => <option key={w.week} value={w.week}>{w.week}{w.finalized ? " (final)" : ""}</option>)}
        </select>
      ) : null}>
      <Err error={error} />
      {!data ? null : !data.week ? (
        <div className="panel">No payroll weeks yet. {can("imports.manage") && <Link href="/imports">Import this week's vendor files</Link>}</div>
      ) : data.run ? (
        <>
          <div className="row" style={{ marginBottom: 12 }}>
            <Badge tone={statusTone(data.run.status)}>{data.run.status}</Badge>
            <span className="muted">Revision {data.run.revision} · rules v{data.run.rule_set_version} · pay date {t.pay_date}</span>
            <Link href={`/payroll/${data.run.id}`}>Open payroll →</Link>
          </div>
          <div className="grid">
            <Stat label="Checks to pay" value={t.total_checks} tone="ok" />
            <Stat label="Payable checks" value={t.positive_checks} money={false} />
            <Stat label="Gross compensation" value={t.gross_compensation} />
            <Stat label="Chargeback recovery" value={t.chargeback_recovery} />
            <Stat label="Held (not applied)" value={t.manual_holds} tone={Number(t.manual_holds) ? "warn" : ""} />
            <Stat label="Room deductions" value={t.room_deductions} />
            <Stat label="Advance deductions" value={t.advance_deductions} />
            <Stat label="Flight deductions" value={t.flight_deductions} />
            {Object.entries(t.company_payables || {}).map(([k, v]) => <Stat key={k} label={`${k} (company payee)`} value={v} />)}
            <Stat label="Open review items" value={`${data.review.blocking} blocking / ${data.review.total}`} money={false} tone={data.review.blocking ? "bad" : ""} />
          </div>
          <div className="panel">
            <h3 style={{ marginTop: 0 }}>Vendor reconciliation</h3>
            <div className="tablewrap"><table>
              <thead><tr><th>Vendor</th><th className="r">Vendor net (after chargebacks)</th><th className="r">Payroll cost</th><th className="r">Company retained</th></tr></thead>
              <tbody>{Object.entries(data.run.reconciliation || {}).map(([v, r]: any) => (
                <tr key={v}><td>{v}</td><td className="r"><Money v={r.vendor_revenue} /></td><td className="r"><Money v={r.payroll_cost} /></td><td className="r"><Money v={r.company_retained} /></td></tr>
              ))}</tbody>
            </table></div>
          </div>
          <div className="panel">
            <h3 style={{ marginTop: 0 }}>Payments</h3>
            {data.payments.length ? data.payments.map((p: any) => <div key={p.state}><Badge tone={statusTone(p.state)}>{p.state}</Badge> {p.n} · <Money v={p.amount} /></div>)
              : <span className="muted">No payments yet. Payments are created when the week is finalized.</span>}
          </div>
          <div className="panel">
            <h3 style={{ marginTop: 0 }}>Imports for {data.week}</h3>
            {data.imports.map((b: any) => (
              <div key={b.id} className="row"><Badge tone={statusTone(b.status)}>{b.status}</Badge><Link href={`/imports/${b.id}`}>{b.file_name}</Link>
                <span className="muted small">{Object.entries(b.counts || {}).map(([k, v]) => `${v} ${k}`).join(", ")}</span></div>
            ))}
          </div>
        </>
      ) : data.mine ? (
        <div className="panel">
          <h3 style={{ marginTop: 0 }}>{data.week}</h3>
          {data.mine.length ? data.mine.map((p: any) => (
            <div key={p.payee} className="row"><strong>{p.payee}</strong><Money v={p.final_check} strong /><Badge tone={statusTone(p.status)}>{p.status}</Badge>
              <Link href={`/statements?week=${data.week}&person=${encodeURIComponent(p.payee)}`}>View statement</Link></div>
          )) : <span className="muted">Nothing for you this week.</span>}
        </div>
      ) : <div className="panel">{data.week} has no calculated run yet.</div>}
    </Shell>
  );
}
