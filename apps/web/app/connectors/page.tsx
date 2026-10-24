"use client";
import { useState } from "react";
import { Badge, Err, Shell, useAction, useLoad } from "@/components/ui";
import { api } from "@/lib/api";

const TARGET_LABEL: Record<string, string> = {
  enrollment_id: "Enrollment ID *", status: "Status *", raw_rep: "Sales rep", sale_date: "Sale date", approval_date: "Approval date", status_reason: "Status reason",
  customer: "Customer", utility: "Utility", product: "Product", kwh: "kWh", service_address: "Service address", city: "City", state: "State", zip: "ZIP",
};
const EXAMPLE_STEPS = [
  { goto: "https://portal.example.com/login" },
  { fill: "input[name=email]", value: "{username}" },
  { fill: "input[type=password]", value: "{password}" },
  { click: "button[type=submit]" },
  { wait_for: "text=Dashboard" },
  { goto: "https://portal.example.com/enrollments?from={date_from}&to={date_to}" },
  { download: "text=Export" },
];
const blank = { name: "", vendor: "", kind: "portal", hosts: "", steps: JSON.stringify(EXAMPLE_STEPS, null, 1), lookback: 45, apiUrl: "", mapping: {} as Record<string, string>, run_hours: "6", enabled: false, alert_destination: "" };

export default function Connectors() {
  const { data, error, reload } = useLoad<any>("/connectors");
  const [edit, setEdit] = useState<any>(null);
  const [open, setOpen] = useState<string | null>(null);
  const act = useAction();
  const run = (id: string) => act.run(async () => { await api(`/connectors/${id}/run`, { method: "POST" }); setTimeout(reload, 4000); });
  const toForm = (c: any) => ({
    id: c.id, name: c.name, vendor: c.vendor, kind: c.kind, hosts: (c.config.allowed_hosts || []).join(", "), steps: JSON.stringify(c.config.steps || [], null, 1),
    lookback: c.config.lookback_days ?? 45, apiUrl: c.config.url || "", mapping: c.mapping, run_hours: c.run_hours.join(", "), enabled: c.enabled, alert_destination: c.alert_destination || "",
  });
  return (
    <Shell title="Vendor connectors" actions={<button className="primary" onClick={() => setEdit({ ...blank })}>Add connector</button>}>
      <p className="muted" style={{ marginTop: 0 }}>Connectors log in to a vendor portal (or call its API) on a schedule, download the enrollment export and update statuses for the leaderboards and map. They never touch payroll: invoices still come in through Imports and review.</p>
      <Err error={error || act.error} />
      {data && !data.key_set && <div className="alert warn">CONNECTOR_SECRET_KEY is not set on the server, so credentials can't be saved yet. Generate one with <code>python -m emg.cli connector-key</code>.</div>}
      {data && !data.browser_ready && <div className="alert warn">The headless browser isn't installed on the worker (pip install playwright; playwright install chromium).</div>}
      {data?.connectors?.length > 0 && <div className="tablewrap"><table><thead><tr><th>Name</th><th>Vendor</th><th>Type</th><th>Runs at</th><th>Credentials</th><th>Enabled</th><th>Last run</th><th></th></tr></thead>
        <tbody>{data.connectors.map((c: any) => <tr key={c.id}>
          <td><button className="link" onClick={() => setOpen(open === c.id ? null : c.id)}>{c.name}</button></td><td>{c.vendor}</td><td>{c.kind === "portal" ? "Portal login" : "API"}</td>
          <td className="small">{c.run_hours.map((h: number) => `${String(h).padStart(2, "0")}:00`).join(", ")}</td>
          <td>{c.has_credentials ? <Badge tone="ok">saved</Badge> : <Badge tone="warn">missing</Badge>}</td><td>{c.enabled ? <Badge tone="ok">on</Badge> : <Badge tone="muted">off</Badge>}</td>
          <td className="small">{c.last_run ? <><Badge tone={c.last_run.status === "ok" ? "ok" : c.last_run.status === "failed" ? "bad" : "info"}>{c.last_run.status}</Badge> {new Date(c.last_run.started_at).toLocaleString()}</> : <span className="muted">never</span>}</td>
          <td><button onClick={() => run(c.id)} disabled={act.busy || !c.has_credentials}>Run now</button> <button onClick={() => setEdit(toForm(c))}>Edit</button></td></tr>)}</tbody></table></div>}
      {open && <Runs id={open} />}
      {edit && <Editor form={edit} targets={data?.targets || []} onClose={() => { setEdit(null); reload(); }} />}
    </Shell>
  );
}

function Runs({ id }: { id: string }) {
  const { data } = useLoad<any[]>(`/connectors/${id}/runs`, [id]);
  return <><h2>Run history</h2><div className="tablewrap"><table><thead><tr><th>Started</th><th>Trigger</th><th>Result</th><th>Details</th></tr></thead>
    <tbody>{(data || []).map((r) => <tr key={r.id}><td className="small">{new Date(r.started_at).toLocaleString()}</td><td>{r.trigger}</td>
      <td><Badge tone={r.status === "ok" ? "ok" : r.status === "failed" ? "bad" : "info"}>{r.status}</Badge></td>
      <td className="small">{r.error || Object.entries(r.summary).filter(([k]) => k !== "uncategorised_statuses").map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`).join(" · ")}
        {r.summary?.uncategorised_statuses && <div className="warn">Statuses not yet categorised: {Object.keys(r.summary.uncategorised_statuses).join(", ")}</div>}
        {r.has_screenshot && <> · <a href={`/api/connector-runs/${r.id}/screenshot`} target="_blank">screenshot at failure</a></>}</td></tr>)}</tbody></table></div></>;
}

function Editor({ form, targets, onClose }: { form: any; targets: string[]; onClose: () => void }) {
  const [f, setF] = useState<any>(form);
  const [creds, setCreds] = useState({ username: "", password: "", totp_secret: "", token: "" });
  const [test, setTest] = useState<any>(null);
  const act = useAction();
  const body = () => {
    let steps: any[] = [];
    if (f.kind === "portal") { try { steps = JSON.parse(f.steps); } catch { throw new Error("Steps must be valid JSON"); } }
    const mapping = Object.fromEntries(Object.entries(f.mapping).filter(([, v]) => v));
    return {
      name: f.name, vendor: f.vendor, kind: f.kind, mapping, enabled: f.enabled, alert_destination: f.alert_destination || null,
      run_hours: String(f.run_hours).split(",").map((x: string) => Number(x.trim())).filter((x: number) => !Number.isNaN(x)),
      config: f.kind === "portal" ? { allowed_hosts: f.hosts.split(",").map((x: string) => x.trim()).filter(Boolean), steps, lookback_days: Number(f.lookback) }
        : { provider: "http_json", url: f.apiUrl, lookback_days: Number(f.lookback) },
    };
  };
  const save = () => act.run(async () => {
    const saved = await api(f.id ? `/connectors/${f.id}` : "/connectors", { method: f.id ? "PUT" : "POST", json: body() });
    const c = Object.fromEntries(Object.entries(creds).filter(([, v]) => v));
    if (Object.keys(c).length) await api(`/connectors/${saved.id}/credentials`, { method: "PUT", json: c });
    onClose();
  });
  const tryFile = (file: File) => act.run(async () => {
    const fd = new FormData(); fd.append("file", file); fd.append("mapping", JSON.stringify(body().mapping));
    setTest(await api("/connectors/test-mapping", { method: "POST", body: fd }));
  });
  const set = (k: string, v: any) => setF({ ...f, [k]: v });
  return (
    <div className="panel" style={{ marginTop: 16 }}>
      <h3>{f.id ? `Edit ${form.name}` : "New connector"}</h3>
      <Err error={act.error} />
      <div className="row"><input placeholder="Name (e.g. Spark portal)" value={f.name} onChange={(e) => set("name", e.target.value)} />
        <input placeholder="Vendor (as in payroll, e.g. Spark)" value={f.vendor} onChange={(e) => set("vendor", e.target.value)} />
        <select value={f.kind} onChange={(e) => set("kind", e.target.value)}><option value="portal">Portal login</option><option value="api">API</option></select>
        <label className="small">Look back <input type="number" value={f.lookback} onChange={(e) => set("lookback", e.target.value)} style={{ width: 60 }} /> days</label>
        <label className="small">Run at hours <input value={f.run_hours} onChange={(e) => set("run_hours", e.target.value)} style={{ width: 90 }} /></label>
        <label className="chk small"><input type="checkbox" checked={f.enabled} onChange={(e) => set("enabled", e.target.checked)} />enabled</label>
        <input placeholder="Failure alert destination (optional)" value={f.alert_destination} onChange={(e) => set("alert_destination", e.target.value)} /></div>
      {f.kind === "portal" ? <>
        <p className="small muted">The browser may only visit these hosts (comma separated; include the portal's login/SSO host). Steps run in order; <code>{"{username}"}</code>, <code>{"{password}"}</code>, <code>{"{totp}"}</code>, <code>{"{date_from}"}</code>, <code>{"{date_to}"}</code> are filled in at run time. The last step must be a <code>download</code>.</p>
        <input placeholder="portal.vendor.com, login.vendor.com" value={f.hosts} onChange={(e) => set("hosts", e.target.value)} style={{ width: "100%", marginBottom: 6 }} />
        <textarea value={f.steps} onChange={(e) => set("steps", e.target.value)} rows={10} style={{ width: "100%", fontFamily: "monospace", fontSize: 12 }} />
      </> : <input placeholder="https://api.vendor.com/enrollments" value={f.apiUrl} onChange={(e) => set("apiUrl", e.target.value)} style={{ width: "100%" }} />}
      <h3 style={{ marginTop: 10 }}>Columns in the export</h3>
      <div className="grid">{targets.map((t) => <label key={t} className="small">{TARGET_LABEL[t] || t}<input value={f.mapping[t] || ""} onChange={(e) => set("mapping", { ...f.mapping, [t]: e.target.value })} placeholder="column header" /></label>)}</div>
      <div className="row small">Check the mapping with an export you downloaded by hand: <input type="file" accept=".csv,.xlsx" onChange={(e) => e.target.files?.[0] && tryFile(e.target.files[0])} /></div>
      {test && <div className="tablewrap" style={{ maxHeight: 220 }}><p className="small">{test.rows} rows read. First rows:</p><table><thead><tr>{Object.keys(test.sample[0] || {}).map((k) => <th key={k}>{k}</th>)}</tr></thead>
        <tbody>{test.sample.map((r: any, i: number) => <tr key={i}>{Object.values(r).map((v: any, j) => <td key={j} className="small">{String(v ?? "")}</td>)}</tr>)}</tbody></table></div>}
      <h3 style={{ marginTop: 10 }}>Login {f.id && <span className="small muted">(leave blank to keep the saved one)</span>}</h3>
      <p className="small muted">Use a separate read-only portal user. Credentials are encrypted on the server and can't be viewed again.</p>
      <div className="row">{f.kind === "portal" ? <>
        <input placeholder="Username" value={creds.username} onChange={(e) => setCreds({ ...creds, username: e.target.value })} autoComplete="off" />
        <input placeholder="Password" type="password" value={creds.password} onChange={(e) => setCreds({ ...creds, password: e.target.value })} autoComplete="new-password" />
        <input placeholder="Authenticator secret (if the portal uses 2FA)" value={creds.totp_secret} onChange={(e) => setCreds({ ...creds, totp_secret: e.target.value })} autoComplete="off" />
      </> : <input placeholder="API token" type="password" value={creds.token} onChange={(e) => setCreds({ ...creds, token: e.target.value })} autoComplete="new-password" />}</div>
      <div className="row" style={{ marginTop: 10 }}><button className="primary" onClick={save} disabled={act.busy || !f.name || !f.vendor}>Save</button><button onClick={onClose}>Cancel</button></div>
    </div>
  );
}
