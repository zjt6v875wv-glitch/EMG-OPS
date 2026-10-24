"use client";
import { useEffect, useMemo, useState } from "react";
import { Err, Money, Shell, useLoad } from "@/components/ui";
import { useSession } from "@/lib/session";

export default function Ledger() {
  const { user, can } = useSession();
  const [person, setPerson] = useState("");
  useEffect(() => { setPerson(new URLSearchParams(location.search).get("person") || (can("ledger.view_all") ? "" : user?.person_name || "")); }, [user]);
  const { data: all, error } = useLoad<any[]>(can("ledger.view_all") ? "/ledger" : person ? `/ledger?person=${encodeURIComponent(person)}` : null, [person]);
  const { data: hist } = useLoad<any[]>(person ? `/ledger/${encodeURIComponent(person)}/history` : null, [person]);
  const byPerson = useMemo(() => {
    const m: Record<string, Record<string, number>> = {};
    for (const b of all || []) { (m[b.person] ||= {}); m[b.person][b.debt_type === "advance" && b.creditor !== "EMG" ? `advance (${b.creditor})` : b.debt_type] = Number(b.balance); }
    return m;
  }, [all]);
  const cols = Array.from(new Set(Object.values(byPerson).flatMap((r) => Object.keys(r)))).sort();
  return (
    <Shell title={person ? `Ledger · ${person}` : "Ledgers"} actions={person && can("ledger.view_all") ? <button onClick={() => setPerson("")}>All people</button> : null}>
      <p className="muted" style={{ marginTop: 0 }}>Positive = the person owes. Negative = money EMG is holding for them (unapplied holds, clawback reserve). Each person has their own ledger; nothing is combined without an explicit merge.</p>
      <Err error={error} />
      {!person && <div className="tablewrap"><table>
        <thead><tr><th>Person</th>{cols.map((c) => <th key={c} className="r">{c}</th>)}</tr></thead>
        <tbody>{Object.entries(byPerson).map(([p, r]) => <tr key={p} className="click" onClick={() => setPerson(p)}><td>{p}</td>{cols.map((c) => <td key={c} className="r">{r[c] ? <Money v={r[c].toFixed(2)} /> : ""}</td>)}</tr>)}</tbody>
      </table></div>}
      {person && hist && <div className="tablewrap"><table>
        <thead><tr><th>Week</th><th>Debt</th><th>Creditor</th><th>Entry</th><th className="r">Amount</th><th className="r">Running</th><th className="wrap">Note</th><th>Ref</th></tr></thead>
        <tbody>{(() => { const run: Record<string, number> = {}; return hist.map((e) => { const k = `${e.debt_type}:${e.creditor}`; run[k] = (run[k] || 0) + Math.round(Number(e.amount) * 100); return (
          <tr key={e.id}><td>{e.week}</td><td>{e.debt_type}</td><td>{e.creditor}</td><td>{e.entry_type}</td><td className="r"><Money v={e.amount} /></td><td className="r num">{(run[k] / 100).toFixed(2)}</td><td className="wrap small">{e.note}</td><td className="small muted">{e.reference}</td></tr>); }); })()}</tbody>
      </table></div>}
    </Shell>
  );
}
