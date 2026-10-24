"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Err, Money, Shell, WeekInput, shortDateCell, statusTone, useAction, useLoad } from "@/components/ui";
import { api, currentWeek } from "@/lib/api";
import { useSession } from "@/lib/session";

export default function Payroll() {
  const [week, setWeek] = useState(currentWeek());
  const { data, error, reload } = useLoad<any[]>(`/weeks/${week}/runs`, [week]);
  const act = useAction();
  const router = useRouter();
  const { can } = useSession();
  const finalized = data?.find((r) => r.status === "finalized");
  const openCorrection = data?.find((r) => r.status === "calculated" && r.supersedes);
  const calc = () => act.run(async () => { const r = await api(`/weeks/${week}/calculate`, { method: "POST" }); router.push(`/payroll/${r.id}`); });
  const correct = () => {
    const reason = prompt(`Reason for correcting finalized ${week}?`);
    if (reason) act.run(async () => { const r = await api(`/weeks/${week}/corrections`, { method: "POST", json: { reason } }); router.push(`/payroll/${r.id}`); });
  };
  return (
    <Shell title="Payroll" actions={<>
      <WeekInput value={week} onChange={setWeek} />
      {can("payroll.calculate") && (!finalized || openCorrection) && <button className="primary" onClick={calc} disabled={act.busy}>{act.busy ? "Calculating…" : openCorrection ? "Recalculate correction" : "Calculate week"}</button>}
      {can("payroll.correct") && finalized && !openCorrection && <button onClick={correct} disabled={act.busy}>Start correction</button>}
    </>}>
      <p className="muted" style={{ marginTop: 0 }}>Calculating is safe to repeat: each run is a numbered revision and nothing posts to ledgers until you finalize. A finalized week can only change through a correction, which keeps the original revision.</p>
      <Err error={error || act.error} />
      <div className="tablewrap"><table>
        <thead><tr><th>Revision</th><th>Status</th><th className="r">Checks</th><th className="r">Count</th><th>Rules</th><th className="wrap">Note</th><th>Calculated</th><th>Finalized</th></tr></thead>
        <tbody>{(data || []).map((r) => (
          <tr key={r.id} className="click" onClick={() => router.push(`/payroll/${r.id}`)}>
            <td><Link href={`/payroll/${r.id}`}>Rev {r.revision}</Link></td><td><Badge tone={statusTone(r.status)}>{r.status}</Badge></td>
            <td className="r"><Money v={r.totals.total_checks} /></td><td className="r">{r.totals.positive_checks}</td><td>v{r.rule_set_version}</td>
            <td className="wrap small">{r.correction_reason}</td>{shortDateCell(r.created_at)}{shortDateCell(r.finalized_at)}
          </tr>
        ))}</tbody>
      </table></div>
      {data && !data.length && <p className="muted">No runs for {week}. Import and commit the week's files, enter week adjustments, then calculate.</p>}
    </Shell>
  );
}
