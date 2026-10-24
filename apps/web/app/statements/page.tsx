"use client";
import { useEffect, useState } from "react";
import { Badge, Err, Money, Shell, statusTone, useLoad } from "@/components/ui";
import { Statement } from "@/components/statement";
import { useSession } from "@/lib/session";

export default function Statements() {
  const { user } = useSession();
  const [person, setPerson] = useState<string>("");
  const [runId, setRunId] = useState<string>("");
  useEffect(() => {
    const p = new URLSearchParams(location.search);
    setPerson(p.get("person") || user?.person_name || "");
  }, [user]);
  const { data: weeks, error } = useLoad<any[]>(person ? `/statements?person=${encodeURIComponent(person)}` : null, [person]);
  useEffect(() => { if (weeks?.length && !runId) setRunId(weeks[0].run_id); }, [weeks, runId]);
  const { data: detail } = useLoad<any>(runId && person ? `/runs/${runId}/people/${encodeURIComponent(person)}` : null, [runId, person]);
  const { data: files } = useLoad<any[]>(runId ? `/runs/${runId}/reports` : null, [runId]);
  const pdf = files?.find((f) => f.kind === "agent_pdf" && f.person === person);
  return (
    <Shell title={person ? `Statements · ${person}` : "Statements"} actions={weeks?.length ? (
      <select value={runId} onChange={(e) => setRunId(e.target.value)}>{weeks.map((w) => <option key={w.run_id} value={w.run_id}>{w.week} · {w.final_check}</option>)}</select>) : null}>
      <Err error={error} />
      {weeks && !weeks.length && <p className="muted">No finalized statements yet.</p>}
      {detail && <Statement d={detail} pdfUrl={pdf ? `/api/reports/${pdf.id}/download` : undefined} />}
    </Shell>
  );
}
