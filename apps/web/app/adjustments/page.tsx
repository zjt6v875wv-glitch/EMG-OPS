"use client";
import { useState } from "react";
import { Badge, Err, Money, Shell, WeekInput, useAction, useLoad } from "@/components/ui";
import { api, currentWeek } from "@/lib/api";
import { useSession } from "@/lib/session";

const KINDS: Record<string, string> = {
  new_advance: "New advance", room_charge: "Room charge", flight_charge: "Flight charge", other_charge: "Other charge",
  collection: "Collection instruction (this week)", defer: "Defer collection (this week)", protected_check: "Protected minimum check (this week)",
  manual_hold: "Hold pay", cash_credit: "Refund / reimbursement / bonus", reserve: "Clawback reserve",
};
const EMPTY = { person: "", kind: "new_advance", amount: "", method: "all", value: "", debts: ["advance"], creditor: "EMG", label: "", note: "", authorized_by: "", external_ref: "" };

export default function Adjustments() {
  const [week, setWeek] = useState(currentWeek());
  const [showVoid, setShowVoid] = useState(false);
  const { data, error, reload } = useLoad<any[]>(`/weeks/${week}/adjustments?include_void=${showVoid}`, [week, showVoid]);
  const { data: people } = useLoad<any[]>("/people");
  const [f, setF] = useState<any>(EMPTY);
  const act = useAction();
  const { can } = useSession();
  const set = (k: string, v: any) => setF({ ...f, [k]: v });
  const save = () => act.run(async () => {
    await api(`/weeks/${week}/adjustments`, { method: "POST", json: { ...f, amount: f.amount || "0", value: f.value || "0", external_ref: f.external_ref || null } });
    setF({ ...EMPTY, kind: f.kind, authorized_by: f.authorized_by }); reload();
  });
  const voidIt = (id: string) => { const note = prompt("Reason for voiding?"); if (note) act.run(async () => { await api(`/adjustments/${id}/void`, { method: "POST", json: { note } }); reload(); }); };
  const needsAmount = !["collection", "defer"].includes(f.kind) && !(f.kind === "manual_hold" && f.label === "all");
  return (
    <Shell title="Week adjustments" actions={<><WeekInput value={week} onChange={setWeek} /><label className="row small"><input type="checkbox" checked={showVoid} onChange={(e) => setShowVoid(e.target.checked)} /> show voided</label></>}>
      <p className="muted" style={{ marginTop: 0 }}>One-week facts for {week}. They apply to this week only and never become standing rules. Recalculate the week after changes.</p>
      {can("adjustments.manage") && (
        <div className="panel">
          <div className="form">
            <label>Person<input list="people" value={f.person} onChange={(e) => set("person", e.target.value)} /></label>
            <datalist id="people">{(people || []).map((p) => <option key={p.name} value={p.name} />)}</datalist>
            <label>Kind<select value={f.kind} onChange={(e) => set("kind", e.target.value)}>{Object.entries(KINDS).map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></label>
            {needsAmount && <label>Amount<input inputMode="decimal" value={f.amount} onChange={(e) => set("amount", e.target.value)} /></label>}
            {f.kind === "collection" && <>
              <label>Method<select value={f.method} onChange={(e) => set("method", e.target.value)}><option value="all">All available (capped at owed)</option><option value="fixed">Fixed amount</option><option value="percent">Percent of available</option><option value="none">None</option></select></label>
              {f.method !== "all" && f.method !== "none" && <label>{f.method === "percent" ? "Percent" : "Amount"}<input value={f.value} onChange={(e) => set("value", e.target.value)} /></label>}
            </>}
            {["collection", "defer"].includes(f.kind) && <label>Debts<select multiple value={f.debts} onChange={(e) => set("debts", Array.from(e.target.selectedOptions).map((o) => o.value))}>{["room", "advance", "flight", "other"].map((d) => <option key={d}>{d}</option>)}</select></label>}
            {f.kind === "new_advance" && <label>Creditor<input value={f.creditor} onChange={(e) => set("creditor", e.target.value)} /></label>}
            {f.kind === "manual_hold" && <label>Hold<select value={f.label} onChange={(e) => set("label", e.target.value)}><option value="">This amount</option><option value="all">Entire check</option></select></label>}
            {f.kind === "cash_credit" && <label>Type<select value={f.label} onChange={(e) => set("label", e.target.value)}><option value="refund">Refund</option><option value="reimbursement">Reimbursement</option><option value="bonus">Bonus</option><option value="hold_release">Release held pay</option></select></label>}
            <label>Authorized by<input value={f.authorized_by} onChange={(e) => set("authorized_by", e.target.value)} /></label>
            <label>Reference (prevents duplicates)<input placeholder="e.g. transfer screenshot id" value={f.external_ref} onChange={(e) => set("external_ref", e.target.value)} /></label>
            <label style={{ gridColumn: "span 2" }}>Note<input value={f.note} onChange={(e) => set("note", e.target.value)} /></label>
            <button className="primary" disabled={act.busy || !f.person || !f.authorized_by} onClick={save}>Add</button>
          </div>
          <Err error={act.error} />
        </div>
      )}
      <Err error={error} />
      <div className="tablewrap"><table>
        <thead><tr><th>Person</th><th>Kind</th><th className="r">Amount</th><th>Detail</th><th className="wrap">Note</th><th>Authorized</th><th>Ref</th><th></th></tr></thead>
        <tbody>{(data || []).map((a) => (
          <tr key={a.id} style={a.voided_at ? { opacity: 0.5, textDecoration: "line-through" } : undefined}>
            <td>{a.person}</td><td>{KINDS[a.kind] || a.kind}</td><td className="r">{Number(a.amount) ? <Money v={a.amount} /> : ""}</td>
            <td className="small">{[a.method, Number(a.value) ? a.value : "", a.debts?.join("+"), a.creditor !== "EMG" ? a.creditor : "", a.label].filter(Boolean).join(" · ")}</td>
            <td className="wrap small">{a.note}</td><td className="small">{a.authorized_by}</td><td className="small">{a.external_ref}</td>
            <td>{!a.voided_at && can("adjustments.manage") && <button onClick={() => voidIt(a.id)}>Void</button>}{a.voided_at && <Badge>void</Badge>}</td>
          </tr>
        ))}</tbody>
      </table></div>
    </Shell>
  );
}
