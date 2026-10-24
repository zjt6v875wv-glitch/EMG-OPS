"use client";
import { useEffect, useState } from "react";
import { Badge, Err, Money, Shell, WeekInput, statusTone, useAction, useLoad } from "@/components/ui";
import { api, currentWeek } from "@/lib/api";

export default function Payments() {
  const [week, setWeek] = useState(currentWeek());
  const { data: runs } = useLoad<any[]>(`/weeks/${week}/runs`, [week]);
  const final = runs?.find((r) => r.status === "finalized");
  const { data, error, reload } = useLoad<any[]>(final ? `/runs/${final.id}/payments` : null, [final?.id]);
  const act = useAction();
  const move = (id: string, state: string) => {
    const ref = state === "confirmed" ? prompt("Payroll provider reference (e.g. Paychex batch id)") : undefined;
    if (state === "confirmed" && !ref) return;
    act.run(async () => { await api(`/payments/${id}/state`, { method: "POST", json: { state, provider_ref: ref } }); reload(); });
  };
  const bulk = (state: string) => {
    const ref = prompt(`Mark all ${state}. Provider reference:`) || undefined;
    if (state === "confirmed" && !ref) return;
    act.run(async () => { await api(`/runs/${final.id}/payments/state`, { method: "POST", json: { state, provider_ref: ref } }); reload(); });
  };
  const total = (data || []).filter((p) => p.state !== "void").reduce((a, p) => a + Math.round(Number(p.amount) * 100), 0) / 100;
  return (
    <Shell title="Payments" actions={<><WeekInput value={week} onChange={setWeek} />{final && <><button onClick={() => bulk("submitted")}>Mark all submitted</button><button className="primary" onClick={() => bulk("confirmed")}>Confirm all</button></>}</>}>
      <p className="muted" style={{ marginTop: 0 }}>Calculated payroll is not a payment. A payment moves from calculated to submitted to confirmed only when you record it here. Emailing statements never changes a payment.</p>
      <Err error={error || act.error} />
      {runs && !final && <p className="muted">{week} is not finalized yet.</p>}
      {data && <div className="tablewrap"><table>
        <thead><tr><th>Person</th><th className="r">Amount</th><th>State</th><th>Provider ref</th><th>Updated</th><th></th></tr></thead>
        <tbody>{data.map((p) => (
          <tr key={p.id}><td>{p.person}</td><td className="r"><Money v={p.amount} /></td><td><Badge tone={statusTone(p.state)}>{p.state}</Badge></td><td>{p.provider_ref}</td>
            <td className="small muted">{p.updated_by} {new Date(p.updated_at).toLocaleString()}</td>
            <td className="row">{p.state === "calculated" && <button onClick={() => move(p.id, "submitted")}>Submitted</button>}
              {p.state === "submitted" && <><button onClick={() => move(p.id, "confirmed")}>Confirmed</button><button onClick={() => move(p.id, "failed")}>Failed</button></>}
              {p.state === "failed" && <button onClick={() => move(p.id, "submitted")}>Resubmitted</button>}</td></tr>
        ))}</tbody>
        <tfoot><tr><td>{data.length} payments</td><td className="r num">{total.toFixed(2)}</td><td colSpan={4}></td></tr></tfoot>
      </table></div>}
    </Shell>
  );
}
