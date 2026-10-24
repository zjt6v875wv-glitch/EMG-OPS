"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, money } from "@/lib/api";
import { useSession } from "@/lib/session";

const NAV: { href: string; label: string; perm?: string }[] = [
  { href: "/", label: "Dashboard" },
  { href: "/imports", label: "Imports", perm: "imports.manage" },
  { href: "/review", label: "Review queue", perm: "payroll.view_all" },
  { href: "/payroll", label: "Payroll", perm: "payroll.view_all" },
  { href: "/adjustments", label: "Week adjustments", perm: "payroll.view_all" },
  { href: "/payments", label: "Payments", perm: "payments.manage" },
  { href: "/ledger", label: "Ledgers" },
  { href: "/statements", label: "My statements" },
  { href: "/reports", label: "Reports", perm: "reports.build" },
  { href: "/email", label: "Email statements", perm: "email.send" },
  { href: "/rules", label: "Rules", perm: "payroll.view_all" },
  { href: "/people", label: "People", perm: "payroll.view_all" },
  { href: "/map", label: "Sales map", perm: "map.view" },
  { href: "/leaderboards", label: "Leaderboards", perm: "leaderboards.view" },
  { href: "/audit", label: "Audit log", perm: "audit.view" },
  { href: "/users", label: "Users", perm: "users.manage" },
];

export function Shell({ title, actions, children }: { title: string; actions?: React.ReactNode; children: React.ReactNode }) {
  const { user, can, loading } = useSession();
  const path = usePathname();
  const [open, setOpen] = useState(false);
  if (loading) return <div className="center muted">Loading…</div>;
  const items = NAV.filter((n) => !n.perm || can(n.perm)).filter((n) => n.href !== "/statements" || user?.person_name);
  return (
    <div className="layout">
      <aside className={open ? "side open" : "side"}>
        <div className="brand">
          <span className="mark">EMG</span>
          <span>Operations</span>
        </div>
        <nav>
          {items.map((n) => (
            <Link key={n.href} href={n.href} className={(n.href === "/" ? path === "/" : path.startsWith(n.href)) ? "active" : ""} onClick={() => setOpen(false)}>
              {n.label}
            </Link>
          ))}
        </nav>
        {user && (
          <div className="who">
            <div>{user.name}</div>
            <div className="muted small">{user.role}{user.person_name ? ` · ${user.person_name}` : ""}</div>
            <button className="link" onClick={() => api("/auth/logout", { method: "POST" }).then(() => (location.href = "/login"))}>Sign out</button>
          </div>
        )}
      </aside>
      <main>
        <header className="top">
          <button className="menu" aria-label="Menu" onClick={() => setOpen(!open)}>☰</button>
          <h1>{title}</h1>
          <div className="actions">{actions}</div>
        </header>
        <div className="content">{children}</div>
      </main>
    </div>
  );
}

export function useLoad<T>(path: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const reload = useCallback(() => {
    if (!path) return;
    setLoading(true);
    api<T>(path)
      .then((d) => { setData(d); setError(null); })
      .catch((e: ApiError) => setError(e.message))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ...deps]);
  useEffect(reload, [reload]);
  return { data, error, loading, reload, setData };
}

export function Err({ error }: { error?: string | null }) {
  return error ? <div className="alert error">{error}</div> : null;
}

export function Money({ v, strong }: { v: any; strong?: boolean }) {
  const s = money(v);
  const neg = String(v ?? "").startsWith("-");
  return <span className={`num${neg ? " neg" : ""}${strong ? " strong" : ""}`}>{s}</span>;
}

export function Stat({ label, value, money: isMoney = true, tone }: { label: string; value: any; money?: boolean; tone?: string }) {
  return (
    <div className={`stat ${tone || ""}`}>
      <div className="muted small">{label}</div>
      <div className="big">{isMoney ? money(value) : value}</div>
    </div>
  );
}

export function Badge({ children, tone = "" }: { children: React.ReactNode; tone?: string }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

export function statusTone(s: string) {
  return ({ finalized: "ok", PAYABLE: "ok", confirmed: "ok", committed: "ok", calculated: "info", preview: "info", submitted: "info",
    HELD: "warn", open: "warn", superseded: "muted", void: "muted", rejected: "muted", failed: "bad", "NO CHECK": "muted" } as Record<string, string>)[s] || "";
}

export function WeekInput({ value, onChange }: { value: string; onChange: (w: string) => void }) {
  return (
    <input className="week" value={value} pattern="\d{4}-W\d{2}" title="YYYY-Www" onChange={(e) => onChange(e.target.value.toUpperCase())} />
  );
}

export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [items, setItems] = useState<any[] | undefined>();
  const run = async <T,>(fn: () => Promise<T>): Promise<T | undefined> => {
    setBusy(true); setError(null); setItems(undefined);
    try { return await fn(); }
    catch (e: any) { setError(e.message); setItems(e.items); return undefined; }
    finally { setBusy(false); }
  };
  return { busy, error, items, run, setError };
}

export function Json({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}

export function shortDateCell(v?: string | null) {
  return <td className="small muted">{v ? new Date(v).toLocaleString() : ""}</td>;
}
