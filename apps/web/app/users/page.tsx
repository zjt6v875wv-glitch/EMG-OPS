"use client";
import { useState } from "react";
import { Err, Shell, useAction, useLoad } from "@/components/ui";
import { api } from "@/lib/api";

export default function Users() {
  const { data, error, reload } = useLoad<any[]>("/users");
  const [f, setF] = useState({ email: "", name: "", role: "agent", password: "", person_name: "" });
  const act = useAction();
  const save = () => act.run(async () => { await api("/users", { method: "POST", json: { ...f, person_name: f.person_name || null } }); setF({ email: "", name: "", role: "agent", password: "", person_name: "" }); reload(); });
  return (
    <Shell title="Users">
      <div className="panel"><div className="form">
        <label>Email<input value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} /></label>
        <label>Name<input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
        <label>Role<select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>{["owner", "admin", "payroll", "manager", "agent"].map((r) => <option key={r}>{r}</option>)}</select></label>
        <label>Payroll name (manager/agent)<input value={f.person_name} onChange={(e) => setF({ ...f, person_name: e.target.value })} /></label>
        <label>Temporary password<input type="password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} /></label>
        <button className="primary" onClick={save} disabled={act.busy}>Create</button>
      </div><Err error={act.error} /></div>
      <Err error={error} />
      <div className="tablewrap"><table><thead><tr><th>Email</th><th>Name</th><th>Role</th><th>Payroll name</th><th>Active</th></tr></thead>
        <tbody>{(data || []).map((u) => <tr key={u.id}><td>{u.email}</td><td>{u.name}</td><td>{u.role}</td><td>{u.person_name}</td><td>{u.active ? "yes" : "no"}</td></tr>)}</tbody></table></div>
    </Shell>
  );
}
