"use client";
import { useState } from "react";
import { Err, Shell, useLoad } from "@/components/ui";

export default function Audit() {
  const [entity, setEntity] = useState("");
  const [id, setId] = useState("");
  const { data, error } = useLoad<any[]>(`/audit?limit=500${entity ? `&entity=${entity}` : ""}${id ? `&entity_id=${encodeURIComponent(id)}` : ""}`, [entity, id]);
  const [open, setOpen] = useState<number | null>(null);
  return (
    <Shell title="Audit log" actions={<>
      <select value={entity} onChange={(e) => setEntity(e.target.value)}><option value="">All</option>{["payroll_run", "rule_set", "import_batch", "source_file", "week_adjustment", "payment", "review_item", "person", "user", "report", "email"].map((x) => <option key={x}>{x}</option>)}</select>
      <input placeholder="Entity id" value={id} onChange={(e) => setId(e.target.value)} />
    </>}>
      <Err error={error} />
      <div className="tablewrap"><table>
        <thead><tr><th>When</th><th>Who</th><th>Action</th><th>Entity</th><th className="wrap">Note</th></tr></thead>
        <tbody>{(data || []).map((a) => <>
          <tr key={a.id} className="click" onClick={() => setOpen(open === a.id ? null : a.id)}><td className="small">{new Date(a.at).toLocaleString()}</td><td>{a.actor}</td><td>{a.action}</td><td className="small">{a.entity} {a.entity_id}</td><td className="wrap small">{a.note}</td></tr>
          {open === a.id && <tr key={a.id + "d"}><td colSpan={5}><pre className="json">{JSON.stringify({ before: a.before, after: a.after }, null, 2)}</pre></td></tr>}
        </>)}</tbody>
      </table></div>
    </Shell>
  );
}
