"use client";
import { useEffect, useState } from "react";
import { Badge, Err, Shell, WeekInput, statusTone, useAction, useLoad } from "@/components/ui";
import { api, currentWeek } from "@/lib/api";

const KIND_LABEL: Record<string, string> = {
  package_zip: "Full package (ZIP)", master_xlsx: "Master payroll workbook", combined_pdf: "All statements (one PDF)", reports_zip: "Individual statements (ZIP)",
  payable_csv: "Payable checks (CSV)", payable_only_csv: "Payable checks only (CSV)", paychex_csv: "Paychex import (1099-NEC Amount)",
  aeg_workbook: "Company payee: repriced Spark workbook", aeg_invoice_pdf: "Company payee invoice (PDF)", aeg_invoice_csv: "Company payee invoice (CSV)", agent_pdf: "Agent statement",
};
const ORDER = Object.keys(KIND_LABEL);
const kb = (n: number) => (n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.ceil(n / 1e3)} KB`);

export default function Reports() {
  const [week, setWeek] = useState(currentWeek());
  useEffect(() => { const w = new URLSearchParams(location.search).get("week"); if (w) setWeek(w); }, []);
  const { data: runs } = useLoad<any[]>(`/weeks/${week}/runs`, [week]);
  const run = runs?.find((r) => r.status === "finalized") || runs?.find((r) => r.status === "calculated");
  const { data: files, error, reload } = useLoad<any[]>(run ? `/runs/${run.id}/reports` : null, [run?.id]);
  const { data: hist } = useLoad<any[]>(`/weeks/${week}/reports`, [week, files]);
  const [q, setQ] = useState("");
  const act = useAction();
  const build = () => act.run(async () => { await api(`/runs/${run.id}/reports`, { method: "POST", json: {} }); reload(); });
  const pkg = (files || []).filter((f) => f.kind !== "agent_pdf").sort((a, b) => ORDER.indexOf(a.kind) - ORDER.indexOf(b.kind));
  const agents = (files || []).filter((f) => f.kind === "agent_pdf" && (f.person || "").toLowerCase().includes(q.toLowerCase()));
  const older = (hist || []).filter((h) => !h.current || (run && h.run_id !== run.id));
  return (
    <Shell title="Reports" actions={<><WeekInput value={week} onChange={setWeek} />{run && <button className="primary" onClick={build} disabled={act.busy}>{act.busy ? "Building…" : files?.length ? "Rebuild reports" : "Build reports"}</button>}</>}>
      <p className="muted" style={{ marginTop: 0 }}>Files are named by week and Friday pay date. Preliminary and corrected revisions are labelled in the file name. Rebuilding only creates a new version when the content changed, and earlier final revisions stay downloadable below.</p>
      <Err error={error || act.error} />
      {runs && !run && <p className="muted">No calculated or finalized run for {week}.</p>}
      {run && <div className="row" style={{ marginBottom: 12 }}><Badge tone={statusTone(run.status)}>{run.status}</Badge><span className="muted">Revision {run.revision}</span></div>}
      {pkg.length > 0 && <div className="tablewrap"><table>
        <thead><tr><th>Report</th><th>File</th><th>Label</th><th className="r">Size</th><th>Built</th></tr></thead>
        <tbody>{pkg.map((f) => <tr key={f.id}><td>{KIND_LABEL[f.kind] || f.kind}{f.person ? ` · ${f.person}` : ""}</td><td><a href={`/api/reports/${f.id}/download`}>{f.file_name}</a></td>
          <td><Badge tone={f.label?.startsWith("Final") ? "ok" : "info"}>{f.label}</Badge></td><td className="r small">{kb(f.size_bytes)}</td><td className="small muted">{new Date(f.created_at).toLocaleString()}</td></tr>)}</tbody>
      </table></div>}
      {agents.length > 0 || q ? <>
        <h2>Agent statements ({(files || []).filter((f) => f.kind === "agent_pdf").length})</h2>
        <input placeholder="Search person" value={q} onChange={(e) => setQ(e.target.value)} style={{ marginBottom: 8 }} />
        <div className="tablewrap"><table><thead><tr><th>Person</th><th>File</th><th className="r">Size</th></tr></thead>
          <tbody>{agents.map((f) => <tr key={f.id}><td>{f.person}</td><td><a href={`/api/reports/${f.id}/download`}>{f.file_name}</a></td><td className="r small">{kb(f.size_bytes)}</td></tr>)}</tbody></table></div>
      </> : null}
      {older.length > 0 && <><h2>Earlier versions</h2>
        <div className="tablewrap"><table><thead><tr><th>Revision</th><th>Report</th><th>File</th><th>Status</th></tr></thead>
          <tbody>{older.filter((h) => h.kind !== "agent_pdf").map((h) => <tr key={h.id}><td>Rev {h.revision}</td><td>{KIND_LABEL[h.kind] || h.kind}</td><td><a href={`/api/reports/${h.id}/download`}>{h.file_name}</a></td>
            <td>{h.current ? <Badge>kept from earlier revision</Badge> : <Badge tone="muted">replaced</Badge>}</td></tr>)}</tbody></table></div></>}
    </Shell>
  );
}
