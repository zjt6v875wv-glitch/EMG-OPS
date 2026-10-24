"use client";
import dynamic from "next/dynamic";
import { useEffect, useMemo, useState } from "react";
import { Badge, Err, Shell, Stat, useAction, useLoad } from "@/components/ui";
import type { Point } from "@/components/salesmap";
import { api } from "@/lib/api";
import { useSession } from "@/lib/session";

const SalesMap = dynamic(() => import("@/components/salesmap"), { ssr: false, loading: () => <div className="salesmap muted">Loading map…</div> });

const CATS = ["approved", "pending", "needs_action", "denied", "cancelled", "chargeback", "other"];
const CAT_LABEL: Record<string, string> = { approved: "Approved", pending: "Pending", needs_action: "Needs action", denied: "Denied", cancelled: "Cancelled", chargeback: "Charged back", other: "Other", duplicate: "Duplicate" };
const UNMAPPED_LABEL: Record<string, string> = { no_address: "no service address", not_geocoded: "address not located yet", unmatched: "address not found", low_confidence: "low-confidence location", ambiguous: "ambiguous address" };

type Filters = { vendors: string[]; categories: string[]; sellers: string[]; date_from: string; date_to: string; territory: string; out_of_territory: "" | "true" | "false"; q: string; min_confidence: number };

const inBox = (p: Point, b: number[] | null) => !b || (p.lng >= b[0] && p.lng <= b[2] && p.lat >= b[1] && p.lat <= b[3]);

export default function MapPage() {
  const { can } = useSession();
  const [f, setF] = useState<Filters>({ vendors: [], categories: [], sellers: [], date_from: "", date_to: "", territory: "", out_of_territory: "", q: "", min_confidence: 0.8 });
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [bounds, setBounds] = useState<number[] | null>(null);
  const [showAreas, setShowAreas] = useState(true);
  const [focus, setFocus] = useState<Point | null>(null);
  const [vendorsSeen, setVendorsSeen] = useState<string[]>([]);
  const { data: terr, reload: reloadTerr } = useLoad<any>("/territories");

  const body = useMemo(() => ({
    ...f, out_of_territory: f.out_of_territory === "" ? null : f.out_of_territory === "true",
    date_from: f.date_from || null, date_to: f.date_to || null, territory: f.territory || null, q: f.q || null,
  }), [f]);
  const load = () => api("/map/points", { method: "POST", json: body }).then((d) => { setData(d); setError(null); setVendorsSeen((v) => Array.from(new Set([...v, ...d.points.map((p: Point) => p.vendor)])).sort()); }).catch((e) => setError(e.message));
  useEffect(() => { const t = setTimeout(load, 250); return () => clearTimeout(t); }, [body]); // eslint-disable-line react-hooks/exhaustive-deps

  const pts: Point[] = data?.points || [];
  const visible = pts.filter((p) => inBox(p, bounds));
  const kwh = visible.reduce((s, p) => s + (p.kwh ? Number(p.kwh) : 0), 0);
  const byCat = visible.reduce<Record<string, number>>((m, p) => ((m[p.category] = (m[p.category] || 0) + 1), m), {});
  const sellers = Array.from(new Set(pts.map((p) => p.seller))).sort();
  const unmapped = Object.entries<number>(data?.unmapped || {});
  const toggle = (k: "vendors" | "categories", v: string) => setF((x) => ({ ...x, [k]: x[k].includes(v) ? x[k].filter((y) => y !== v) : [...x[k], v] }));
  const search = f.q ? pts.slice(0, 8) : [];

  const exportCsv = async (visibleOnly: boolean) => {
    const res = await fetch("/api/map/export", { method: "POST", credentials: "include", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...body, bbox: visibleOnly ? bounds : null }) });
    if (!res.ok) return setError((await res.json()).detail);
    const a = document.createElement("a"); a.href = URL.createObjectURL(await res.blob()); a.download = "EMG_Sales_Map.csv"; a.click();
  };

  return (
    <Shell title="Sales map" actions={<><button onClick={() => exportCsv(true)}>Export visible area</button><button onClick={() => exportCsv(false)}>Export all filtered</button></>}>
      <Err error={error} />
      <div className="mapfilters">
        <div className="row">{vendorsSeen.map((v) => <label key={v} className="chk"><input type="checkbox" checked={f.vendors.includes(v)} onChange={() => toggle("vendors", v)} />{v}</label>)}</div>
        <div className="row">{CATS.map((c) => <button key={c} className={`chip${f.categories.includes(c) ? " on" : ""}`} onClick={() => toggle("categories", c)}>
          <span className="dot" style={{ background: data?.colors?.[c] }} />{CAT_LABEL[c]}</button>)}</div>
        <div className="row">
          <select value={f.sellers[0] || ""} onChange={(e) => setF({ ...f, sellers: e.target.value ? [e.target.value] : [] })}><option value="">All sellers</option>{sellers.map((s) => <option key={s}>{s}</option>)}</select>
          <label className="small">Sold <input type="date" value={f.date_from} onChange={(e) => setF({ ...f, date_from: e.target.value })} /> to <input type="date" value={f.date_to} onChange={(e) => setF({ ...f, date_to: e.target.value })} /></label>
          <select value={f.territory} onChange={(e) => setF({ ...f, territory: e.target.value })}><option value="">Any area</option>{(terr?.territories || []).filter((t: any) => t.active).map((t: any) => <option key={t.id}>{t.name}</option>)}</select>
          <select value={f.out_of_territory} onChange={(e) => setF({ ...f, out_of_territory: e.target.value as Filters["out_of_territory"] })}><option value="">In or out of territory</option><option value="true">Outside territory only</option><option value="false">Inside territory only</option></select>
          <label className="chk"><input type="checkbox" checked={showAreas} onChange={(e) => setShowAreas(e.target.checked)} />Show areas</label>
        </div>
        <div className="row" style={{ position: "relative" }}>
          <input placeholder={data?.show_customer ? "Search customer, address, enrollment, seller, ZIP" : "Search enrollment, seller, city, ZIP"} value={f.q} onChange={(e) => setF({ ...f, q: e.target.value })} style={{ minWidth: 320 }} />
          {search.length > 0 && <div className="small">{search.map((p) => <button key={p.id} className="link" onClick={() => setFocus(p)}>{p.vendor} {p.enrollment_id}{p.customer ? ` · ${p.customer}` : ""}</button>)}</div>}
        </div>
      </div>
      <div className="grid">
        <Stat label="In view" value={visible.length} money={false} />
        <Stat label="kWh in view" value={kwh.toLocaleString(undefined, { maximumFractionDigits: 0 })} money={false} />
        <Stat label="Approved in view" value={byCat.approved || 0} money={false} tone="ok" />
        <Stat label="Outside territory in view" value={visible.filter((p) => p.out_of_territory).length} money={false} tone={visible.some((p) => p.out_of_territory) ? "bad" : ""} />
      </div>
      {data && <SalesMap points={pts} colors={data.colors} territories={terr?.territories || []} showTerritories={showAreas} tileUrl={data.tile_url} attribution={data.tile_attribution} onBounds={setBounds} focus={focus} />}
      <p className="small muted">
        {Object.entries(byCat).map(([k, n]) => `${CAT_LABEL[k] || k} ${n}`).join(" · ") || "No points in view."}
        {unmapped.length > 0 && <> Not on the map: {unmapped.map(([k, n]) => `${n} ${UNMAPPED_LABEL[k] || k}`).join(", ")}. Locations are never guessed.</>}
        {" "}Showing locations at {Math.round((data?.min_confidence ?? 0.8) * 100)}% confidence or better, plus manual placements.
      </p>
      {can("territories.manage") && <MapData onChange={() => { load(); reloadTerr(); }} territories={terr} />}
      {!can("territories.manage") && terr?.capacity?.length > 0 && <Capacity rows={terr.capacity} />}
    </Shell>
  );
}

function Capacity({ rows }: { rows: any[] }) {
  return <><h2>Capacity</h2><div className="tablewrap"><table><thead><tr><th>Area</th><th>Vendor</th><th className="r">Capacity kWh</th><th className="r">Allocated</th><th className="r">of which estimated</th><th className="r">Remaining</th><th className="r">Enrollments</th></tr></thead>
    <tbody>{rows.map((r) => <tr key={r.id}><td>{r.name}</td><td>{r.vendor || "Any"}</td><td className="r">{r.capacity_kwh ?? "not set"}</td><td className="r">{r.allocated_kwh}</td><td className="r">{r.estimated_kwh}</td>
      <td className="r">{r.over_capacity ? <Badge tone="bad">{r.remaining_kwh}</Badge> : r.remaining_kwh ?? ""}</td><td className="r">{r.enrollments}</td></tr>)}</tbody></table></div></>;
}

function MapData({ onChange, territories }: { onChange: () => void; territories: any }) {
  const act = useAction();
  const [batch, setBatch] = useState<any>(null);
  const [vendor, setVendor] = useState("");
  const [tKind, setTKind] = useState("territory");
  const [tVendor, setTVendor] = useState("");
  const [geo, setGeo] = useState<any>(null);
  const { data: queue, reload: reloadQueue } = useLoad<any>("/map/geocode/queue");

  const upload = (file: File) => act.run(async () => {
    const fd = new FormData(); fd.append("file", file); if (vendor) fd.append("vendor", vendor);
    setBatch(await api("/map/enrich", { method: "POST", body: fd }));
  });
  const apply = () => act.run(async () => { const r = await api(`/map/enrich/${batch.id}/apply`, { method: "POST" }); setBatch({ ...batch, status: "applied", applied: r }); onChange(); reloadQueue(); });
  const terrUpload = (file: File) => act.run(async () => {
    const fd = new FormData(); fd.append("file", file); fd.append("kind", tKind); if (tVendor) fd.append("vendor", tVendor);
    await api("/territories", { method: "POST", body: fd }); onChange();
  });
  const runGeo = () => act.run(async () => { setGeo(await api("/map/geocode/run", { method: "POST" })); });
  const patch = (id: string, b: any) => act.run(async () => { await api(`/territories/${id}`, { method: "PATCH", json: b }); onChange(); });

  return (
    <>
      <h2>Map data</h2>
      <Err error={act.error} />
      <div className="panel">
        <h3>Service addresses</h3>
        <p className="small muted">Vendor payroll files carry no service addresses. Upload a project status report (choose its vendor), an address mapping export (matched to enrollments by customer and sale date within 3 days), or a CSV of Vendor, Enrollment ID, Latitude, Longitude. You see the plan before anything changes.</p>
        <div className="row"><input placeholder="Vendor (status reports)" value={vendor} onChange={(e) => setVendor(e.target.value)} />
          <input type="file" accept=".xlsx,.csv" onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} disabled={act.busy} /></div>
        {batch && <div style={{ marginTop: 8 }}>
          <div className="row"><Badge tone="info">{batch.kind}</Badge>{Object.entries(batch.summary).map(([k, v]) => <span key={k} className="small">{k}: <b>{String(v)}</b></span>)}
            {batch.status === "previewed" ? <button className="primary" onClick={apply} disabled={act.busy}>Apply</button> : <Badge tone="ok">applied</Badge>}</div>
          <div className="tablewrap" style={{ maxHeight: 260 }}><table><thead><tr><th>Row</th><th>Action</th><th>Enrollment</th><th>Detail</th></tr></thead>
            <tbody>{batch.plan.filter((p: any) => p.action !== "unchanged").slice(0, 300).map((p: any) => <tr key={p.row}><td>{p.row}</td><td><Badge tone={{ enrich: "ok", create: "info", locate: "ok", unmatched: "warn", ambiguous: "warn", skip: "muted" }[p.action as string] || ""}>{p.action}</Badge></td>
              <td>{p.enrollment_id ? `${p.vendor} ${p.enrollment_id}` : ""}</td><td className="small">{p.reason || p.note || Object.entries(p.fields || {}).map(([k, v]) => `${k}: ${v}`).join("; ")}</td></tr>)}</tbody></table></div>
        </div>}
      </div>
      <div className="panel">
        <h3>Locations</h3>
        <p className="small muted">Addresses are located by the configured geocoder (US Census by default) and cached with a confidence score. Unmatched addresses stay off the map until someone places them.</p>
        <div className="row"><button onClick={runGeo} disabled={act.busy}>Locate new addresses</button>{geo && <span className="small muted">Queued job {String(geo.id).slice(0, 8)}; refresh in a minute.</span>}
          {queue?.summary?.map((s: any) => <span key={s.s} className="small">{UNMAPPED_LABEL[s.s] || s.s}: <b>{s.n}</b></span>)}</div>
        {queue?.rows?.length > 0 && <div className="tablewrap" style={{ maxHeight: 220 }}><table><thead><tr><th>Enrollment</th><th>Address</th><th>Result</th><th>Place manually</th></tr></thead>
          <tbody>{queue.rows.map((r: any) => <ManualRow key={`${r.vendor}${r.enrollment_id}`} r={r} onDone={() => { reloadQueue(); onChange(); }} />)}</tbody></table></div>}
      </div>
      <div className="panel">
        <h3>Territories and projects</h3>
        <p className="small muted">KML, GeoJSON, or CSV (name + WKT, or name/lat/lng vertex rows). Sales outside every territory that applies to their vendor are flagged. A GeoJSON property <code>capacity_kwh</code> turns on capacity tracking.</p>
        <div className="row"><select value={tKind} onChange={(e) => setTKind(e.target.value)}><option value="territory">Territory</option><option value="project">Project</option></select>
          <input placeholder="Vendor (blank = all)" value={tVendor} onChange={(e) => setTVendor(e.target.value)} />
          <input type="file" accept=".kml,.geojson,.json,.csv" onChange={(e) => e.target.files?.[0] && terrUpload(e.target.files[0])} disabled={act.busy} /></div>
        {territories?.territories?.length > 0 && <div className="tablewrap"><table><thead><tr><th>Name</th><th>Kind</th><th>Vendor</th><th>Active</th><th>Capacity kWh</th></tr></thead>
          <tbody>{territories.territories.map((t: any) => <tr key={t.id}><td>{t.name}</td><td>{t.kind}</td><td>{t.vendor || "Any"}</td>
            <td><input type="checkbox" checked={t.active} onChange={(e) => patch(t.id, { active: e.target.checked })} /></td>
            <td><input style={{ width: 120 }} defaultValue={t.capacity_kwh ?? ""} placeholder="off" onBlur={(e) => e.target.value !== String(t.capacity_kwh ?? "") && patch(t.id, e.target.value ? { capacity_kwh: e.target.value, capacity_enabled: true } : { capacity_enabled: false })} /></td></tr>)}</tbody></table></div>}
      </div>
      {territories?.capacity?.length > 0 && <Capacity rows={territories.capacity} />}
    </>
  );
}

function ManualRow({ r, onDone }: { r: any; onDone: () => void }) {
  const [v, setV] = useState("");
  const act = useAction();
  const save = () => act.run(async () => {
    const [lat, lng] = v.split(",").map((x) => Number(x.trim()));
    await api("/map/location", { method: "POST", json: { vendor: r.vendor, enrollment_id: r.enrollment_id, lat, lng } }); onDone();
  });
  return <tr><td>{r.vendor} {r.enrollment_id}</td><td className="small">{r.service_address}, {r.city} {r.state} {r.zip}</td><td><Badge tone="warn">{r.geocode_status}</Badge></td>
    <td><input placeholder="lat, lng" value={v} onChange={(e) => setV(e.target.value)} style={{ width: 170 }} /><button onClick={save} disabled={!v || act.busy}>Save</button>{act.error && <span className="small bad">{act.error}</span>}</td></tr>;
}
