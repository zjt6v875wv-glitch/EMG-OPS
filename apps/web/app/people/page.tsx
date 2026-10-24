"use client";
import { useState } from "react";
import { Badge, Err, Shell, useAction, useLoad } from "@/components/ui";
import { api } from "@/lib/api";
import { useSession } from "@/lib/session";

export default function People() {
  const { data, error, reload } = useLoad<any[]>("/people");
  const [q, setQ] = useState("");
  const [edit, setEdit] = useState<any>(null);
  const act = useAction();
  const { can } = useSession();
  const save = () => act.run(async () => { await api("/people", { method: "POST", json: edit }); setEdit(null); reload(); });
  const merge = () => {
    const source = prompt("Merge which person (their ledger moves)?"); if (!source) return;
    const target = prompt(`Merge ${source} into?`); if (!target) return;
    const reason = prompt("Reason (required)"); if (!reason) return;
    act.run(async () => { await api("/people/merge", { method: "POST", json: { source, target, reason } }); reload(); });
  };
  const rows = (data || []).filter((p) => p.name.toLowerCase().includes(q.toLowerCase()));
  return (
    <Shell title="People" actions={<>
      <input placeholder="Search" value={q} onChange={(e) => setQ(e.target.value)} />
      {can("people.manage") && <button onClick={() => setEdit({ name: "", kind: "agent", email: "", email_verified: false, email_excluded: false })}>Add person</button>}
      {can("people.merge") && <button onClick={merge}>Merge…</button>}
    </>}>
      <p className="muted" style={{ marginTop: 0 }}>Payroll identities and statement emails. Only verified emails are used for sending; addresses are never guessed. Aliases and temporary ownership live in the rules.</p>
      <Err error={error || act.error} />
      {edit && <div className="panel"><div className="form">
        <label>Name<input value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} /></label>
        <label>Kind<select value={edit.kind} onChange={(e) => setEdit({ ...edit, kind: e.target.value })}><option>agent</option><option>manager</option><option>company</option></select></label>
        <label>Email<input type="email" value={edit.email || ""} onChange={(e) => setEdit({ ...edit, email: e.target.value })} /></label>
        <label className="row"><input type="checkbox" checked={edit.email_verified} onChange={(e) => setEdit({ ...edit, email_verified: e.target.checked })} /> Email verified</label>
        <label className="row"><input type="checkbox" checked={edit.email_excluded} onChange={(e) => setEdit({ ...edit, email_excluded: e.target.checked })} /> Do not email</label>
        <label>Notes<input value={edit.notes || ""} onChange={(e) => setEdit({ ...edit, notes: e.target.value })} /></label>
        <div className="row"><button className="primary" onClick={save}>Save</button><button onClick={() => setEdit(null)}>Cancel</button></div>
      </div></div>}
      <div className="tablewrap"><table>
        <thead><tr><th>Name</th><th>Kind</th><th>Email</th><th>Email status</th><th>Status</th><th className="wrap">Notes</th><th></th></tr></thead>
        <tbody>{rows.map((p) => (
          <tr key={p.id}><td>{p.name}</td><td>{p.kind}</td><td>{p.email}</td>
            <td>{p.email_excluded ? <Badge tone="muted">excluded</Badge> : p.email_verified ? <Badge tone="ok">verified</Badge> : p.email ? <Badge tone="warn">unverified</Badge> : <Badge tone="bad">missing</Badge>}</td>
            <td>{p.merged_into ? `merged into ${p.merged_into}` : p.status}</td><td className="wrap small">{p.notes}</td>
            <td>{can("people.manage") && <button onClick={() => setEdit({ ...p })}>Edit</button>}</td></tr>
        ))}</tbody>
      </table></div>
    </Shell>
  );
}
