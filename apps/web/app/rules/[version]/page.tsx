"use client";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { Badge, Err, Money, Shell, WeekInput, useAction, useLoad } from "@/components/ui";
import { api, currentWeek } from "@/lib/api";
import { useSession } from "@/lib/session";

const SECTIONS = ["agent_rates", "memberships", "override_rules", "personal_targets", "routing_rules", "badge_rules", "temporary_assignments", "collection_rules", "aliases", "utility_groups", "vendors", "people", "deduction_order", "commercial"];

function Section({ name, rows }: { name: string; rows: any }) {
  if (!Array.isArray(rows) || !rows.length || typeof rows[0] !== "object") return <pre className="json">{JSON.stringify(rows, null, 2)}</pre>;
  const cols = Array.from(new Set(rows.flatMap((r: any) => Object.keys(r))));
  return (
    <div className="tablewrap"><table>
      <thead><tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr></thead>
      <tbody>{rows.map((r: any, i: number) => <tr key={i}>{cols.map((c) => <td key={c} className={c === "note" ? "wrap small" : "small"}>{Array.isArray(r[c]) ? r[c].join(", ") : r[c] === null || r[c] === undefined ? "" : String(r[c])}</td>)}</tr>)}</tbody>
    </table></div>
  );
}

export default function RuleVersion() {
  const { version } = useParams<{ version: string }>();
  const { data, error, reload } = useLoad<any>(`/rules/${version}`);
  const { data: diff, reload: reloadDiff } = useLoad<any>(`/rules/${version}/diff`);
  const [tab, setTab] = useState("agent_rates");
  const [text, setText] = useState("");
  const [week, setWeek] = useState(currentWeek());
  const [preview, setPreview] = useState<any>(null);
  const act = useAction();
  const { can } = useSession();
  useEffect(() => { if (data) setText(JSON.stringify(data.config, null, 2)); }, [data]);
  if (!data) return <Shell title={`Rules v${version}`}><Err error={error} /></Shell>;
  const draft = data.status === "draft";
  const save = () => act.run(async () => {
    let cfg; try { cfg = JSON.parse(text); } catch (e: any) { throw new Error(`Not valid JSON: ${e.message}`); }
    await api(`/rules/${version}`, { method: "PUT", json: { config: cfg, note: prompt("What changed?") || "" } }); reload(); reloadDiff();
  });
  const runPreview = () => act.run(async () => setPreview(await api(`/rules/${version}/preview?week=${week}`, { method: "POST" })));
  const activate = () => { const note = prompt("Activation note (required)"); if (note) act.run(async () => { await api(`/rules/${version}/activate`, { method: "POST", json: { note } }); reload(); }); };
  return (
    <Shell title={`Rules v${version} · ${data.label}`} actions={<>
      <Badge tone={data.status === "active" ? "ok" : draft ? "info" : "muted"}>{data.status}</Badge>
      {draft && can("rules.activate") && <button className="primary" onClick={activate} disabled={act.busy}>Activate</button>}
    </>}>
      <Err error={act.error} />
      {diff && <div className="panel"><h3 style={{ marginTop: 0 }}>Changes from v{diff.from}</h3>
        {diff.changes.length === 0 ? <span className="muted">No changes.</span> : diff.changes.map((c: any, i: number) => (
          <div key={i} className="small"><span className={c.change === "added" ? "diff-add" : c.change === "removed" ? "diff-del" : ""}>{c.change}</span> {c.section}: <code>{JSON.stringify(c.rule || c.after)}</code></div>))}
      </div>}
      {draft && can("rules.edit") && <div className="panel">
        <h3 style={{ marginTop: 0 }}>Preview affected checks</h3>
        <div className="row"><span className="muted">Week</span><WeekInput value={week} onChange={setWeek} /><button onClick={runPreview} disabled={act.busy}>Preview</button></div>
        {preview && <div style={{ marginTop: 8 }}>
          <div>Total checks <Money v={preview.totals.before} /> → <Money v={preview.totals.after} strong /> · {preview.affected_people.length} people change</div>
          {preview.affected_people.map((c: any) => <div key={c.payee} className="small"><strong>{c.payee}</strong>: {Object.entries(c.diffs).map(([k, d]: any) => <span key={k}> {k} <span className="diff-del">{d.before}</span>→<span className="diff-add">{d.after}</span>;</span>)}</div>)}
        </div>}
      </div>}
      <div className="tabs">{[...SECTIONS, ...(draft ? ["edit"] : [])].map((s) => <button key={s} className={tab === s ? "on" : ""} onClick={() => setTab(s)}>{s === "edit" ? "Edit JSON" : s.replace(/_/g, " ")}{Array.isArray(data.config[s]) ? ` (${data.config[s].length})` : ""}</button>)}</div>
      {tab === "edit" ? <>
        <p className="muted small">Each rule can carry first_week / last_week (YYYY-Www) and status. Agent rates are chosen by specificity: agent, then utility group, then product; equal-specificity rules with different rates go to review.</p>
        <textarea className="code" value={text} onChange={(e) => setText(e.target.value)} />
        <button className="primary" onClick={save} disabled={act.busy}>Save draft</button>
      </> : <Section name={tab} rows={data.config[tab]} />}
    </Shell>
  );
}
