"use client";
import { useParams } from "next/navigation";
import { useState } from "react";
import { Badge, Err, Json, Money, Shell, statusTone, useAction, useLoad } from "@/components/ui";
import { api } from "@/lib/api";

export default function Batch() {
  const { id } = useParams<{ id: string }>();
  const { data: b, error, reload } = useLoad<any>(`/imports/${id}`);
  const [type, setType] = useState<string>("");
  const [dups, setDups] = useState(false);
  const { data: rows } = useLoad<any[]>(`/imports/${id}/rows?limit=200${type ? `&line_type=${type}` : ""}${dups ? "&duplicates=true" : ""}`, [type, dups]);
  const act = useAction();
  if (!b) return <Shell title="Import"><Err error={error} /></Shell>;
  const s = b.summary || {};
  const commit = (accept: boolean) => act.run(async () => { await api(`/imports/${id}/commit`, { method: "POST", json: { accept_errors: accept } }); reload(); });
  const reject = () => { const note = prompt("Why reject this preview?"); if (note) act.run(async () => { await api(`/imports/${id}/reject`, { method: "POST", json: { note } }); reload(); }); };
  return (
    <Shell title={b.file_name} actions={b.status === "preview" ? (
      <>
        <button onClick={reject} disabled={act.busy}>Reject</button>
        {b.errors.length ? <button className="primary" onClick={() => commit(true)} disabled={act.busy}>Commit; send {b.errors.length} errors to review</button>
          : <button className="primary" onClick={() => commit(false)} disabled={act.busy}>Commit import</button>}
      </>
    ) : null}>
      <Err error={act.error} />
      <div className="row" style={{ marginBottom: 12 }}>
        <Badge tone={statusTone(b.status)}>{b.status}</Badge>
        <span className="muted">{b.importer} · {b.vendor} · week {b.week} · sha256 {String(b.sha256).slice(0, 12)}…</span>
        <a href={`/api/files/${b.source_file_id}`}>Download original</a>
      </div>
      {s.same_file_committed_batches && <div className="alert warn">This exact file was already committed. Every money line in it will be refused as a duplicate.</div>}
      {s.already_imported_lines > 0 && <div className="alert warn">{s.already_imported_lines} money lines were already imported from another file and will not be counted again.</div>}
      {s.committed && <div className="alert ok">Committed: {s.committed.lines} new lines, {s.committed.refused_duplicates} refused duplicates, {s.committed.status_changes} status changes.</div>}
      <div className="grid">
        {b.line_counts.map((c: any) => <div key={c.line_type} className="stat"><div className="muted small">{c.line_type}</div><div className="big">{c.n}</div>{c.dup ? <div className="small neg">{c.dup} already imported</div> : null}</div>)}
        <div className="stat"><div className="muted small">Excluded (visible)</div><div className="big">{b.excluded.length}</div></div>
        <div className="stat"><div className="muted small">Errors</div><div className="big">{b.errors.length}</div></div>
      </div>
      {s.meta?.vendor_total && <p>Vendor total on file: <Money v={s.meta.vendor_total} /></p>}
      <h2>Column mapping</h2>
      <div className="tablewrap"><table>
        <thead><tr><th>Field</th><th>Source column</th><th>Required</th><th>Sample</th></tr></thead>
        <tbody>{(s.mapping || []).map((m: any) => <tr key={m.target}><td>{m.target}</td><td>{m.source || <span className="neg">not found</span>}</td><td>{m.required ? "yes" : ""}</td><td className="muted">{m.sample}</td></tr>)}</tbody>
      </table></div>
      {b.errors.length > 0 && <><h2>Errors</h2><Json value={b.errors} /></>}
      {b.excluded.length > 0 && <><h2>Excluded rows</h2><p className="muted small">Counted and listed, never silently dropped (for example prior-year sales).</p><Json value={b.excluded.slice(0, 200)} /></>}
      <h2>Rows</h2>
      <div className="row" style={{ marginBottom: 8 }}>
        <select value={type} onChange={(e) => setType(e.target.value)}><option value="">All types</option>{b.line_counts.map((c: any) => <option key={c.line_type}>{c.line_type}</option>)}</select>
        <label className="row small"><input type="checkbox" checked={dups} onChange={(e) => setDups(e.target.checked)} /> Duplicates only</label>
      </div>
      <div className="tablewrap"><table>
        <thead><tr><th>#</th><th>Type</th><th>Enrollment</th><th>Customer ID</th><th>Transaction</th><th>Rep</th><th>Customer</th><th>Utility</th><th>Product</th><th className="r">kWh</th><th className="r">Amount</th><th>Sale date</th><th>Approval</th><th>Invoice</th><th>Status</th><th>Source</th></tr></thead>
        <tbody>{(rows || []).map((r) => { const p = r.payload; return (
          <tr key={r.id} style={r.duplicate_of ? { opacity: 0.55 } : undefined}>
            <td>{r.row_no}</td><td>{r.line_type}{r.duplicate_of ? <Badge tone="warn">dup</Badge> : null}</td><td>{p.enrollment_id}</td><td>{p.customer_id}</td><td>{p.transaction_id}</td><td>{p.raw_rep}</td><td>{p.customer}</td><td>{p.utility}</td><td>{p.product}</td>
            <td className="r num">{p.kwh}</td><td className="r"><Money v={p.vendor_amount} /></td><td>{p.sale_date}</td><td>{p.approval_date}</td><td>{p.invoice_date}</td><td className="wrap small">{[p.status, p.stage, p.notes].filter(Boolean).join(" · ")}</td><td className="small muted">{p.source_sheet} r{p.source_row}</td>
          </tr>); })}</tbody>
      </table></div>
    </Shell>
  );
}
