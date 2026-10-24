"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Err, Shell, WeekInput, shortDateCell, statusTone, useAction, useLoad } from "@/components/ui";
import { api, currentWeek } from "@/lib/api";

const IMPORTERS = [
  ["", "Detect automatically"],
  ["arcadia_perch", "Arcadia / Perch commission statement"],
  ["nexamp_sales_report", "Nexamp sales report (status + dates)"],
  ["nexamp_commissions", "Nexamp commissions (+ chargebacks)"],
  ["spark", "Spark EMGD2D invoice"],
  ["generic", "Other vendor (CSV / XLSX / PDF, mapped)"],
];

const SAMPLE_OPTIONS = JSON.stringify({
  vendor: "Ampion",
  mapping: { enrollment_id: "Account Number", raw_rep: "Sales Rep", kwh: "kWh", vendor_amount: "Commission", sale_date: "Signed Date", status: "Status", utility: "Utility", product: "Product" },
  payable_statuses: ["Approved"], nonpayable_statuses: ["Cancelled", "Rejected"],
}, null, 2);

export default function Imports() {
  const [week, setWeek] = useState(currentWeek());
  const [importer, setImporter] = useState("");
  const [options, setOptions] = useState(SAMPLE_OPTIONS);
  const [file, setFile] = useState<File | null>(null);
  const { data, error, reload } = useLoad<any[]>(`/imports?week=${week}`, [week]);
  const act = useAction();
  const router = useRouter();
  const upload = () => act.run(async () => {
    const fd = new FormData();
    fd.append("file", file!);
    fd.append("week", week);
    if (importer) fd.append("importer", importer);
    if (importer === "generic") fd.append("options", options);
    const b = await api("/imports", { method: "POST", body: fd });
    router.push(`/imports/${b.id}`);
  });
  return (
    <Shell title="Imports" actions={<><span className="muted">Payroll week</span><WeekInput value={week} onChange={setWeek} /></>}>
      <div className="panel">
        <p className="muted" style={{ marginTop: 0 }}>The original file is stored unchanged. You get a preview of mappings, counts, errors, exclusions and already-imported lines before anything is committed. Import the Nexamp sales report before Nexamp commissions so sale dates resolve.</p>
        <div className="form">
          <label>File<input type="file" accept=".xlsx,.xlsm,.csv,.pdf" onChange={(e) => setFile(e.target.files?.[0] || null)} /></label>
          <label>Importer<select value={importer} onChange={(e) => setImporter(e.target.value)}>{IMPORTERS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
          <button className="primary" disabled={!file || act.busy} onClick={upload}>{act.busy ? "Reading…" : "Upload and preview"}</button>
        </div>
        {importer === "generic" && (
          <label className="small muted" style={{ display: "block", marginTop: 12 }}>Column mapping (target field → column label in the file)
            <textarea className="code" style={{ minHeight: 200 }} value={options} onChange={(e) => setOptions(e.target.value)} />
          </label>
        )}
        <Err error={act.error} />
      </div>
      <Err error={error} />
      <div className="tablewrap"><table>
        <thead><tr><th>File</th><th>Importer</th><th>Status</th><th>Rows</th><th>Errors</th><th>By</th><th>When</th></tr></thead>
        <tbody>{(data || []).map((b) => (
          <tr key={b.id} className="click" onClick={() => router.push(`/imports/${b.id}`)}>
            <td><Link href={`/imports/${b.id}`}>{b.file_name}</Link></td><td>{b.importer}</td><td><Badge tone={statusTone(b.status)}>{b.status}</Badge></td>
            <td className="small">{Object.entries(b.summary?.counts || {}).map(([k, v]) => `${v} ${k}`).join(", ")}</td>
            <td>{b.errors ? <Badge tone="bad">{b.errors}</Badge> : ""}</td><td>{b.created_by}</td>{shortDateCell(b.created_at)}
          </tr>
        ))}</tbody>
      </table></div>
      {data && !data.length && <p className="muted">No files imported for {week} yet.</p>}
      <button style={{ marginTop: 12 }} onClick={reload}>Refresh</button>
    </Shell>
  );
}
