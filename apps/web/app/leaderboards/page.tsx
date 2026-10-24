"use client";
import { Fragment, useEffect, useMemo, useState } from "react";
import { Badge, Err, Shell, useAction, useLoad } from "@/components/ui";
import { api } from "@/lib/api";
import { useSession } from "@/lib/session";

const METRICS: Record<string, string> = { written: "Written (by sale date)", approved: "Newly approved (by approval date)", written_approved: "Written & now approved" };
const today = () => new Date().toLocaleDateString("en-CA");
const shift = (d: string, days: number) => { const t = new Date(d + "T12:00:00"); t.setDate(t.getDate() + days); return t.toLocaleDateString("en-CA"); };

function Change({ r }: { r: any }) {
  if (r.change === null) return <Badge tone="info">new</Badge>;
  if (r.change === 0) return <span className="muted">–</span>;
  return <span className={r.change > 0 ? "up" : "down"}>{r.change > 0 ? `▲ ${r.change}` : `▼ ${-r.change}`}</span>;
}

export default function Leaderboards() {
  const { can } = useSession();
  const [cadence, setCadence] = useState("daily");
  const [day, setDay] = useState(today());
  const [metric, setMetric] = useState("written");
  const [group, setGroup] = useState("agent");
  const [vendors, setVendors] = useState("");
  const [top, setTop] = useState(10);
  const [open, setOpen] = useState<string | null>(null);
  const qs = useMemo(() => new URLSearchParams({ cadence, day, metric, group, top: String(top), ...(vendors ? { vendors } : {}) }).toString(), [cadence, day, metric, group, vendors, top]);
  const { data, error } = useLoad<any>(`/leaderboards?${qs}`, [qs]);
  const { data: drill, error: drillErr } = useLoad<any>(open ? `/leaderboards/drilldown?${qs}&name=${encodeURIComponent(open)}` : null, [open, qs]);
  useEffect(() => setOpen(null), [qs]);
  const step = cadence === "daily" ? 1 : 7;

  return (
    <Shell title="Leaderboards" actions={<>{["png", "pdf", "csv"].map((x) => <a key={x} className="button" href={`/api/leaderboards/export.${x}?${qs}`}>{x.toUpperCase()}</a>)}</>}>
      <div className="row" style={{ marginBottom: 12 }}>
        <select value={cadence} onChange={(e) => setCadence(e.target.value)}><option value="daily">Daily</option><option value="weekly">Weekly (Mon–Sun)</option></select>
        <button onClick={() => setDay(shift(day, -step))}>‹</button><input type="date" value={day} onChange={(e) => setDay(e.target.value)} /><button onClick={() => setDay(shift(day, step))}>›</button>
        <select value={metric} onChange={(e) => setMetric(e.target.value)}>{Object.entries(METRICS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
        <select value={group} onChange={(e) => setGroup(e.target.value)}><option value="agent">Agents</option><option value="manager">Managers (team totals)</option></select>
        <input placeholder="Vendors (comma separated)" value={vendors} onChange={(e) => setVendors(e.target.value)} style={{ width: 200 }} />
        <select value={top} onChange={(e) => setTop(Number(e.target.value))}>{[10, 25, 50].map((n) => <option key={n} value={n}>Top {n}</option>)}</select>
      </div>
      <Err error={error} />
      {data && <>
        <p className="muted" style={{ marginTop: 0 }}>{data.metric_label} · {data.period_start === data.period_end ? data.period_start : `${data.period_start} to ${data.period_end}`} · {data.timezone} · {data.total_enrollments} unique enrollments, {data.people_ranked} {group === "manager" ? "managers" : "agents"} with sales. Change compares with {data.previous_start === data.previous_end ? data.previous_start : `${data.previous_start} to ${data.previous_end}`}.</p>
        {data.proxies.map((p: any) => <div key={p.source} className="alert warn">{p.count} of these approvals use an estimate: {p.label}.</div>)}
        <div className="tablewrap"><table className="lbtable">
          <thead><tr><th>Rank</th><th></th><th>Name</th><th className="r">Count</th>{metric === "written" && <th className="r">Approved now</th>}{metric === "approved" && <th className="r">Also written this period</th>}<th>By vendor</th></tr></thead>
          <tbody>
            {data.rows.length === 0 && <tr><td colSpan={6} className="muted">No sales in this period.</td></tr>}
            {data.rows.map((r: any) => <Fragment key={r.name}>
              <tr onClick={() => setOpen(open === r.name ? null : r.name)} style={{ cursor: "pointer" }}>
                <td className="rank">{r.rank}</td><td><Change r={r} /></td><td>{r.name}</td><td className="r"><b>{r.count}</b></td>
                {metric === "written" && <td className="r">{r.approved_now}</td>}{metric === "approved" && <td className="r">{r.also_written_in_period}</td>}
                <td className="small muted">{Object.entries(r.by_vendor).map(([v, n]) => `${v} ${n}`).join(" · ")}</td></tr>
              {open === r.name && <tr><td colSpan={6}>
                <Err error={drillErr} />
                {drill && <div className="tablewrap" style={{ maxHeight: 280 }}><table><thead><tr><th>Vendor</th><th>Enrollment</th><th>Seller</th><th>Sale date</th><th>Status</th><th>Approved</th></tr></thead>
                  <tbody>{drill.rows.map((d: any) => <tr key={d.vendor + d.enrollment_id}><td>{d.vendor}</td><td>{can("payroll.view_all") ? <a href={`/enrollments/${encodeURIComponent(d.vendor)}/${encodeURIComponent(d.enrollment_id)}`}>{d.enrollment_id}</a> : d.enrollment_id}</td>
                    <td>{d.seller}</td><td>{d.sale_date}</td><td>{d.status}</td><td>{d.approved_on}{d.approval_source && d.approval_source !== "vendor" ? <span className="small muted"> (estimated)</span> : ""}</td></tr>)}</tbody></table></div>}
              </td></tr>}
            </Fragment>)}
          </tbody></table></div>
        <p className="small muted">{data.notes.join(" ")} Click a row to see its enrollments.</p>
      </>}
      {can("leaderboards.publish") && <Schedules />}
    </Shell>
  );
}

function Schedules() {
  const { data, reload } = useLoad<any>("/leaderboards/settings");
  const act = useAction();
  const [draft, setDraft] = useState<any>({ name: "", cadence: "daily", metrics: ["written", "approved"], groups: ["agent", "manager"], run_hour: 8, run_weekday: 0, top_n: 10, enabled: false, publish_enabled: false, destination: "" });
  const [dest, setDest] = useState({ name: "", type: "webhook", url: "", enabled: false });
  const [tz, setTz] = useState("");
  useEffect(() => { if (data) setTz(data.timezone); }, [data]);
  const save = () => act.run(async () => { await api("/leaderboards/schedules", { method: "POST", json: { ...draft, destination: draft.destination || null } }); reload(); });
  const toggle = (s: any, k: string) => act.run(async () => { await api(`/leaderboards/schedules/${s.id}`, { method: "PUT", json: { ...s, destination: s.destination?.name || null, [k]: !s[k] } }); reload(); });
  const runNow = (s: any) => act.run(async () => { await api(`/leaderboards/schedules/${s.id}/run`, { method: "POST" }); reload(); });
  // Saving an existing name with an empty URL keeps its stored URL (stored URLs are never sent back in full).
  const addDest = () => act.run(async () => { await api("/leaderboards/destinations", { method: "PUT", json: dest }); reload(); });
  const saveTz = () => act.run(async () => { await api("/leaderboards/timezone", { method: "PUT", json: { timezone: tz } }); reload(); });
  const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const pick = (k: "metrics" | "groups", v: string) => setDraft((d: any) => ({ ...d, [k]: d[k].includes(v) ? d[k].filter((x: string) => x !== v) : [...d[k], v] }));
  if (!data) return null;
  return (
    <>
      <h2>Schedules and publishing</h2>
      <Err error={act.error} />
      <p className="small muted">Scheduled boards cover the last complete day or Monday–Sunday week. They are always generated and kept here; they are published only when the schedule allows publishing <b>and</b> its destination is enabled.</p>
      <div className="row" style={{ marginBottom: 8 }}><label className="small">Business timezone <input value={tz} onChange={(e) => setTz(e.target.value)} /></label><button onClick={saveTz} disabled={act.busy || tz === data.timezone}>Save</button></div>
      <div className="tablewrap"><table><thead><tr><th>Name</th><th>Cadence</th><th>Boards</th><th>Runs at</th><th>Destination</th><th>Enabled</th><th>Publish</th><th>Last run</th><th></th></tr></thead>
        <tbody>{data.schedules.map((s: any) => <tr key={s.id}><td>{s.name}</td><td>{s.cadence}</td><td className="small">{s.metrics.join(", ")} · {s.groups.join(", ")} · top {s.top_n}</td>
          <td className="small">{s.cadence === "weekly" ? days[s.run_weekday] + " " : ""}{String(s.run_hour).padStart(2, "0")}:00 {s.timezone}</td><td>{s.destination?.name || <span className="muted">none</span>}</td>
          <td><input type="checkbox" checked={s.enabled} onChange={() => toggle(s, "enabled")} /></td><td><input type="checkbox" checked={s.publish_enabled} onChange={() => toggle(s, "publish_enabled")} /></td>
          <td className="small muted">{s.last_run_at ? new Date(s.last_run_at).toLocaleString() : ""}</td><td><button onClick={() => runNow(s)} disabled={act.busy}>Generate last period</button></td></tr>)}</tbody></table></div>
      <div className="panel"><h3>New schedule</h3><div className="row">
        <input placeholder="Name" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
        <select value={draft.cadence} onChange={(e) => setDraft({ ...draft, cadence: e.target.value })}><option value="daily">Daily</option><option value="weekly">Weekly</option></select>
        {draft.cadence === "weekly" && <select value={draft.run_weekday} onChange={(e) => setDraft({ ...draft, run_weekday: Number(e.target.value) })}>{days.map((d, i) => <option key={d} value={i}>{d}</option>)}</select>}
        <label className="small">at <input type="number" min={0} max={23} value={draft.run_hour} onChange={(e) => setDraft({ ...draft, run_hour: Number(e.target.value) })} style={{ width: 60 }} />:00</label>
        {Object.keys(METRICS).map((m) => <label key={m} className="chk small"><input type="checkbox" checked={draft.metrics.includes(m)} onChange={() => pick("metrics", m)} />{m.replace("_", " ")}</label>)}
        {["agent", "manager"].map((g) => <label key={g} className="chk small"><input type="checkbox" checked={draft.groups.includes(g)} onChange={() => pick("groups", g)} />{g}s</label>)}
        <select value={draft.destination} onChange={(e) => setDraft({ ...draft, destination: e.target.value })}><option value="">No destination (generate only)</option>{data.destinations.map((d: any) => <option key={d.name}>{d.name}</option>)}</select>
        <label className="chk small"><input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />enabled</label>
        <label className="chk small"><input type="checkbox" checked={draft.publish_enabled} onChange={(e) => setDraft({ ...draft, publish_enabled: e.target.checked })} />publish</label>
        <button className="primary" onClick={save} disabled={act.busy || !draft.metrics.length || !draft.groups.length}>Add</button></div></div>
      <div className="panel"><h3>Destinations</h3>
        {data.destinations.map((d: any) => <div key={d.name} className="small"><button className="link" onClick={() => setDest({ name: d.name, type: d.type, url: "", enabled: !d.enabled })}>{d.name}</button> · {d.type} {d.url} · {d.enabled ? <Badge tone="ok">enabled</Badge> : <Badge tone="muted">disabled</Badge>}</div>)}
        <div className="row" style={{ marginTop: 6 }}><input placeholder="Name" value={dest.name} onChange={(e) => setDest({ ...dest, name: e.target.value })} />
          <select value={dest.type} onChange={(e) => setDest({ ...dest, type: e.target.value })}><option value="webhook">Webhook (Slack/Teams)</option><option value="outbox">Outbox (store only)</option></select>
          {dest.type === "webhook" && <input placeholder="https://hooks…" value={dest.url} onChange={(e) => setDest({ ...dest, url: e.target.value })} style={{ width: 280 }} />}
          <label className="chk small"><input type="checkbox" checked={dest.enabled} onChange={(e) => setDest({ ...dest, enabled: e.target.checked })} />enabled</label>
          <button onClick={addDest} disabled={act.busy || !dest.name}>Save destination</button></div></div>
      {data.runs.length > 0 && <><h3>Generated</h3><div className="tablewrap"><table><thead><tr><th>Period</th><th>Files</th><th>Published</th></tr></thead>
        <tbody>{data.runs.map((r: any) => <tr key={r.id}><td>{r.period_start}{r.period_end !== r.period_start ? ` to ${r.period_end}` : ""}</td>
          <td className="small">{r.files.filter((f: any) => f.kind === "png").map((f: any) => <a key={f.file_name} href={`/api/leaderboards/runs/${r.id}/files/${r.files.indexOf(f)}`} style={{ marginRight: 8 }}>{f.metric.replace("_", " ")} · {f.group}</a>)}</td>
          <td>{r.published ? <Badge tone="ok">published</Badge> : <span className="small muted">{r.publish_result?.reason || r.publish_result?.error || "not published"}</span>}</td></tr>)}</tbody></table></div></>}
    </>
  );
}
