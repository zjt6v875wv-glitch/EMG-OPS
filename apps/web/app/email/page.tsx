"use client";
import { useEffect, useMemo, useState } from "react";
import { Badge, Err, Money, Shell, WeekInput, useAction, useLoad } from "@/components/ui";
import { api, currentWeek } from "@/lib/api";

const STATE: Record<string, [string, string]> = {
  ready: ["Ready", "ok"], ready_new_version: ["Ready (replaces earlier version)", "warn"], already_sent: ["Sent", "info"],
  missing_email: ["No email", "bad"], unverified_email: ["Email not verified", "bad"], excluded: ["Excluded", "muted"],
  authorized: ["Queued", "info"], sending: ["Sending", "info"], unknown: ["Outcome unknown - check", "bad"],
};

export default function Email() {
  const [week, setWeek] = useState(currentWeek());
  const { data: runs } = useLoad<any[]>(`/weeks/${week}/runs`, [week]);
  const run = runs?.find((r) => r.status === "finalized");
  const { data, error, reload } = useLoad<any>(run ? `/runs/${run.id}/email` : null, [run?.id]);
  const { data: log, reload: reloadLog } = useLoad<any[]>(run ? `/runs/${run.id}/email/sends` : null, [run?.id]);
  const { data: tpl, reload: reloadTpl } = useLoad<any>("/email/template");
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [confirm, setConfirm] = useState("");
  const [showTpl, setShowTpl] = useState(false);
  const [t, setT] = useState<any>(null);
  const [result, setResult] = useState<any>(null);
  const act = useAction();
  useEffect(() => { setSel(new Set()); setResult(null); }, [run?.id]);
  useEffect(() => { if (tpl) setT(tpl); }, [tpl]);
  const rows: any[] = data?.rows || [];
  const sendable = useMemo(() => rows.filter((r) => r.state === "ready" || r.state === "ready_new_version"), [rows]);
  const toggle = (p: string) => { const s = new Set(sel); s.has(p) ? s.delete(p) : s.add(p); setSel(s); };
  const send = () => act.run(async () => {
    const r = await api(`/runs/${run.id}/email/authorize`, { method: "POST", json: { people: Array.from(sel), confirm } });
    setResult(r); setSel(new Set()); setConfirm(""); reload(); reloadLog();
  });
  const mark = (s: any, status: string) => { const detail = prompt(`Mark ${s.person}'s email as ${status}. Note:`) ?? ""; act.run(async () => { await api("/email/events", { method: "POST", json: { message_id: s.provider_message_id, status, detail } }); reload(); reloadLog(); }); };
  const exclude = (person: string, excluded: boolean) => { const reason = excluded ? prompt(`Why exclude ${person} from statement email?`) : ""; if (excluded && !reason) return; act.run(async () => { await api("/email/exclusions", { method: "POST", json: { person, reason, excluded } }); reload(); }); };
  const saveTpl = () => act.run(async () => { await api("/email/template", { method: "PUT", json: t }); reloadTpl(); setShowTpl(false); });
  return (
    <Shell title="Email statements" actions={<><WeekInput value={week} onChange={setWeek} /><button onClick={() => setShowTpl(!showTpl)}>Message template</button></>}>
      <p className="muted" style={{ marginTop: 0 }}>Each email goes to one verified address with only that person's own statement attached. There are no CC or BCC copies, and addresses are never guessed. Nothing is sent until you select people and type the confirmation. Sending a statement does not submit a payment.</p>
      <Err error={error || act.error} />
      {data && !data.send_enabled && <div className="alert warn">Email delivery is in outbox mode (EMAIL_SEND_ENABLED is off). Messages are written to storage and are not delivered.</div>}
      {showTpl && t && <div className="panel">
        <div className="form" style={{ gridTemplateColumns: "1fr" }}>
          <label>Subject<input value={t.subject} onChange={(e) => setT({ ...t, subject: e.target.value })} /></label>
          <label>Body<textarea className="code" style={{ minHeight: 200 }} value={t.body} onChange={(e) => setT({ ...t, body: e.target.value })} /></label>
          <div className="small muted">Placeholders: {"{first_name} {name} {week_no} {pay_date} {pay_date_long} {check} {check_line}"}</div>
          <div className="row"><button className="primary" onClick={saveTpl}>Save template</button></div>
        </div>
      </div>}
      {runs && !run && <p className="muted">{week} is not finalized. Statements can be emailed after finalization.</p>}
      {data && <>
        <div className="grid">
          {Object.entries(data.counts).map(([k, v]: any) => <div key={k} className="stat"><div className="muted small">{STATE[k]?.[0] || k}</div><div className="big">{v}</div></div>)}
        </div>
        {data.paid_without_report.length > 0 && <div className="alert error">Paid but no statement built: {data.paid_without_report.join(", ")}. Build reports first.</div>}
        {result && <div className="alert ok">Queued {result.queued.length}. {result.refused.length ? `Skipped: ${result.refused.map((r: any) => `${r.person} (${r.reason})`).join("; ")}` : ""}</div>}
        <div className="panel row">
          <button onClick={() => setSel(new Set(sendable.map((r) => r.person)))}>Select all ready ({sendable.length})</button>
          <button onClick={() => setSel(new Set())}>Clear</button>
          <span className="muted">{sel.size} selected</span>
          <input placeholder={`Type SEND ${data.week}`} value={confirm} onChange={(e) => setConfirm(e.target.value)} />
          <button className="primary" disabled={!sel.size || confirm !== `SEND ${data.week}` || act.busy} onClick={send}>Send {sel.size} statement{sel.size === 1 ? "" : "s"}</button>
        </div>
        <div className="tablewrap"><table>
          <thead><tr><th></th><th>Person</th><th>Recipient</th><th>State</th><th className="r">Check</th><th>Attachment</th><th className="wrap">Note</th><th></th></tr></thead>
          <tbody>{rows.map((r) => { const ok = r.state === "ready" || r.state === "ready_new_version"; const [label, tone] = STATE[r.state] || [r.state, ""]; return (
            <tr key={r.person}>
              <td><input type="checkbox" disabled={!ok} checked={sel.has(r.person)} onChange={() => toggle(r.person)} /></td>
              <td>{r.person}</td><td className="small">{r.email || <span className="neg">missing</span>}</td><td><Badge tone={tone}>{label}</Badge></td>
              <td className="r">{Number(r.final_check) ? <Money v={r.final_check} /> : <span className="muted small">{r.pay_status === "HELD" ? "held" : "no check"}</span>}</td>
              <td className="small"><a href={`/api/reports/${r.report_version_id}/download`}>{r.file_name}</a></td><td className="wrap small">{r.reason}</td>
              <td>{r.state === "excluded" ? <button onClick={() => exclude(r.person, false)}>Include</button> : <button onClick={() => exclude(r.person, true)}>Exclude</button>}</td>
            </tr>); })}</tbody>
        </table></div>
      </>}
      {log && log.length > 0 && <><h2>Send log</h2>
        <div className="tablewrap"><table>
          <thead><tr><th>Person</th><th>Recipient</th><th>Status</th><th>File</th><th>Report hash</th><th>Message ID</th><th>Authorized</th><th>Sent</th><th></th></tr></thead>
          <tbody>{log.map((s) => <tr key={s.id}><td>{s.person}</td><td className="small">{s.recipient}</td><td><Badge tone={s.status === "delivered" || s.status === "sent" ? "ok" : s.status === "bounced" || s.status === "failed" || s.status === "unknown" ? "bad" : "info"}>{s.status}</Badge>{s.error && <div className="small neg">{s.error}</div>}</td>
            <td className="small">{s.file_name}</td><td className="small muted">{s.report_sha256.slice(0, 10)}…</td><td className="small muted">{s.provider_message_id}</td>
            <td className="small">{s.authorized_by}<br />{new Date(s.authorized_at).toLocaleString()}</td><td className="small">{s.sent_at ? new Date(s.sent_at).toLocaleString() : ""}</td>
            <td className="row">{["sent", "unknown"].includes(s.status) && <><button onClick={() => mark(s, "delivered")}>Delivered</button><button onClick={() => mark(s, "bounced")}>Bounced</button></>}
              {s.status === "unknown" && <button onClick={() => mark(s, "sent")}>Confirm sent</button>}</td></tr>)}</tbody>
        </table></div></>}
    </Shell>
  );
}
