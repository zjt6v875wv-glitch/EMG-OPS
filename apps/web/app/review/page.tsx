"use client";
import { useState } from "react";
import { Badge, Err, Money, Shell, WeekInput, useAction, useLoad } from "@/components/ui";
import { api, currentWeek } from "@/lib/api";
import { useSession } from "@/lib/session";

export default function Review() {
  const [week, setWeek] = useState(currentWeek());
  const [status, setStatus] = useState("open");
  const [kind, setKind] = useState("");
  const { data, error, reload } = useLoad<any[]>(`/review?status=${status}${week ? `&week=${week}` : ""}${kind ? `&kind=${kind}` : ""}`, [week, status, kind]);
  const act = useAction();
  const { can } = useSession();
  const kinds = Array.from(new Set((data || []).map((r) => r.kind)));
  const resolve = (id: string, st: string) => {
    const resolution = prompt(st === "open" ? "Why reopen?" : "Resolution note (what was decided and by whom)");
    if (resolution) act.run(async () => { await api(`/review/${id}/resolve`, { method: "POST", json: { resolution, status: st } }); reload(); });
  };
  return (
    <Shell title="Review queue" actions={<>
      <WeekInput value={week} onChange={setWeek} />
      <select value={status} onChange={(e) => setStatus(e.target.value)}><option value="open">Open</option><option value="resolved">Resolved</option><option value="dismissed">Dismissed</option></select>
      <select value={kind} onChange={(e) => setKind(e.target.value)}><option value="">All kinds</option>{kinds.map((k) => <option key={k}>{k}</option>)}</select>
    </>}>
      <p className="muted" style={{ marginTop: 0 }}>Nothing is dropped silently. Blocking items stop finalization until someone records a decision. Resolving an item records the decision; it does not change any amount by itself.</p>
      <Err error={error || act.error} />
      <div className="tablewrap"><table>
        <thead><tr><th></th><th>Kind</th><th className="wrap">Message</th><th>Vendor</th><th>Enrollment</th><th>Person</th><th className="r">Amount</th><th>Week</th><th></th></tr></thead>
        <tbody>{(data || []).map((r) => (
          <tr key={r.id}>
            <td>{r.blocking ? <Badge tone="bad">blocking</Badge> : <Badge>info</Badge>}</td>
            <td>{r.kind}</td><td className="wrap">{r.message}{r.resolution && <div className="small muted">→ {r.resolution} ({r.resolved_by})</div>}</td>
            <td>{r.vendor}</td><td>{r.enrollment_id && r.vendor ? <a href={`/enrollments/${r.vendor}/${encodeURIComponent(r.enrollment_id)}`}>{r.enrollment_id}</a> : r.enrollment_id}</td>
            <td>{r.person}</td><td className="r">{Number(r.amount) ? <Money v={r.amount} /> : ""}</td><td>{r.week}</td>
            <td>{can("review.resolve") && (r.status === "open"
              ? <div className="row"><button onClick={() => resolve(r.id, "resolved")}>Resolve</button><button onClick={() => resolve(r.id, "dismissed")}>Dismiss</button></div>
              : <button onClick={() => resolve(r.id, "open")}>Reopen</button>)}</td>
          </tr>
        ))}</tbody>
      </table></div>
      {data && !data.length && <p className="muted">No {status} items.</p>}
    </Shell>
  );
}
