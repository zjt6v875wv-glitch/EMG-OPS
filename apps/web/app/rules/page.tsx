"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Badge, Err, Shell, shortDateCell, statusTone, useAction, useLoad } from "@/components/ui";
import { api } from "@/lib/api";
import { useSession } from "@/lib/session";

export default function Rules() {
  const { data, error } = useLoad<any[]>("/rules");
  const act = useAction();
  const router = useRouter();
  const { can } = useSession();
  const draft = () => { const label = prompt("Name this draft (e.g. 'Spark rates from W45')"); if (label) act.run(async () => { const r = await api("/rules/drafts", { method: "POST", json: { label } }); router.push(`/rules/${r.version}`); }); };
  return (
    <Shell title="Rules" actions={can("rules.edit") ? <button className="primary" onClick={draft}>New draft from active rules</button> : null}>
      <p className="muted" style={{ marginTop: 0 }}>Rates, teams, overrides, routing and collection rules are versioned data. Edit a draft, preview which checks it changes for a week, then activate it with a note. Every finalized run records the rule version it used.</p>
      <Err error={error || act.error} />
      <div className="tablewrap"><table>
        <thead><tr><th>Version</th><th>Status</th><th>Label</th><th className="wrap">Change note</th><th>Created</th><th>Activated</th></tr></thead>
        <tbody>{(data || []).map((r) => (
          <tr key={r.version} className="click" onClick={() => router.push(`/rules/${r.version}`)}>
            <td><Link href={`/rules/${r.version}`}>v{r.version}</Link></td><td><Badge tone={statusTone(r.status === "active" ? "finalized" : r.status === "draft" ? "calculated" : r.status)}>{r.status}</Badge></td>
            <td>{r.label}</td><td className="wrap small">{r.change_note}</td>{shortDateCell(r.created_at)}<td className="small muted">{r.activated_by} {r.activated_at ? new Date(r.activated_at).toLocaleString() : ""}</td>
          </tr>
        ))}</tbody>
      </table></div>
    </Shell>
  );
}
