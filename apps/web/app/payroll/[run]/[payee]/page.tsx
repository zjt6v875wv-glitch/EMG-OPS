"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { Err, Shell, useLoad } from "@/components/ui";
import { Statement } from "@/components/statement";

export default function PersonInRun() {
  const { run, payee } = useParams<{ run: string; payee: string }>();
  const name = decodeURIComponent(payee);
  const { data, error } = useLoad<any>(`/runs/${run}/people/${encodeURIComponent(name)}`);
  return (
    <Shell title={name} actions={<><Link className="btn" href={`/payroll/${run}`}>← Run</Link><Link className="btn" href={`/ledger?person=${encodeURIComponent(name)}`}>Ledger</Link></>}>
      <Err error={error} />
      {data && <Statement d={data} />}
    </Shell>
  );
}
