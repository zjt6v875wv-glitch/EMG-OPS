"use client";
// Leaflet map with clustered, status-coloured markers and territory outlines.
// Leaflet touches `window`, so it is loaded only in the browser.
import { useEffect, useRef } from "react";
import "leaflet/dist/leaflet.css";
import "leaflet.markercluster/dist/MarkerCluster.css";
import "leaflet.markercluster/dist/MarkerCluster.Default.css";

export type Point = {
  id: string; vendor: string; enrollment_id: string; lat: number; lng: number; status: string; category: string; seller: string;
  sale_date?: string; approval_date?: string; kwh?: string; project?: string; city?: string; state?: string; zip?: string;
  confidence: number; geocode: string; areas: string[]; out_of_territory: boolean | null; customer?: string; address?: string;
};

const esc = (s: unknown) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]!));

function popup(p: Point) {
  const rows: [string, unknown][] = [
    ["Enrollment", `${p.vendor} ${p.enrollment_id}`], ["Customer", p.customer], ["Address", p.address ? `${p.address}, ${p.city ?? ""} ${p.state ?? ""} ${p.zip ?? ""}` : `${p.city ?? ""} ${p.state ?? ""}`],
    ["Seller", p.seller], ["Status", p.status], ["Sale date", p.sale_date], ["Approved", p.approval_date], ["kWh", p.kwh], ["Project", p.project],
    ["Areas", p.areas.join(", ")], ["Territory", p.out_of_territory === null ? "" : p.out_of_territory ? "OUTSIDE territory" : "inside"],
    ["Location", `${p.geocode}${p.geocode === "manual" ? "" : ` (${Math.round(p.confidence * 100)}%)`}`],
  ];
  return `<table class="pop">${rows.filter(([, v]) => v).map(([k, v]) => `<tr><th>${k}</th><td>${esc(v)}</td></tr>`).join("")}</table>
    <a href="/enrollments/${encodeURIComponent(p.vendor)}/${encodeURIComponent(p.enrollment_id)}">Open enrollment</a>`;
}

export default function SalesMap({ points, colors, territories, showTerritories, tileUrl, attribution, onBounds, focus }: {
  points: Point[]; colors: Record<string, string>; territories: any[]; showTerritories: boolean; tileUrl: string; attribution: string;
  onBounds: (b: [number, number, number, number]) => void; focus?: Point | null;
}) {
  const el = useRef<HTMLDivElement>(null);
  const st = useRef<any>({});

  useEffect(() => {
    let alive = true;
    (async () => {
      const L = (await import("leaflet")).default;
      (window as any).L = L;
      await import("leaflet.markercluster");
      if (!alive || !el.current || st.current.map) return;
      const map = L.map(el.current, { preferCanvas: true }).setView([39.5, -98.35], 4);
      L.tileLayer(tileUrl, { attribution, maxZoom: 19 }).addTo(map);
      const report = () => { const b = map.getBounds(); onBounds([b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]); };
      map.on("moveend", report);
      st.current = { L, map, cluster: (L as any).markerClusterGroup({
        chunkedLoading: true, maxClusterRadius: 50,
        // Neutral cluster bubbles so they are not mistaken for status colours.
        iconCreateFunction: (cl: any) => {
          const n = cl.getChildCount(); const size = n < 10 ? 30 : n < 100 ? 38 : 46;
          return L.divIcon({ className: "", iconSize: [size, size], html: `<div class="cluster" style="width:${size}px;height:${size}px">${n}</div>` });
        },
      }).addTo(map), areas: L.layerGroup().addTo(map), byId: {}, fitted: false, report };
      st.current.ready = true;
      draw(); drawAreas();
    })();
    return () => { alive = false; st.current.map?.remove(); st.current = {}; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const draw = () => {
    const { L, map, cluster } = st.current;
    if (!L) return;
    cluster.clearLayers();
    st.current.byId = {};
    const ms = points.map((p) => {
      const ring = p.out_of_territory ? "box-shadow:0 0 0 2px #fff,0 0 0 4px #C53030;" : "box-shadow:0 0 0 2px #fff;";
      const icon = L.divIcon({ className: "", iconSize: [14, 14], html: `<span style="display:block;width:14px;height:14px;border-radius:50%;background:${colors[p.category] || "#555"};${ring}"></span>` });
      const m = L.marker([p.lat, p.lng], { icon, title: `${p.vendor} ${p.enrollment_id}` }).bindPopup(popup(p), { maxWidth: 360 });
      st.current.byId[p.id] = m;
      return m;
    });
    cluster.addLayers(ms);
    if (ms.length && !st.current.fitted) {
      map.fitBounds(L.latLngBounds(points.map((p) => [p.lat, p.lng])), { padding: [30, 30], maxZoom: 14 });
      st.current.fitted = true;
    }
    st.current.report();
  };
  const drawAreas = () => {
    const { L, areas } = st.current;
    if (!L) return;
    areas.clearLayers();
    if (!showTerritories) return;
    for (const t of territories) {
      if (!t.active || !t.geojson) continue;
      L.geoJSON(t.geojson, { style: { color: t.kind === "project" ? "#087F8C" : "#173247", weight: 2, fillOpacity: 0.06, dashArray: t.kind === "project" ? "4 4" : undefined } })
        .bindTooltip(`${t.name}${t.vendor ? ` · ${t.vendor}` : ""}`, { sticky: true }).addTo(areas);
    }
  };
  useEffect(() => { if (st.current.ready) draw(); }, [points, colors]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (st.current.ready) drawAreas(); }, [territories, showTerritories]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const m = focus && st.current.byId?.[focus.id];
    if (m) st.current.cluster.zoomToShowLayer(m, () => m.openPopup());
  }, [focus]);

  return <div ref={el} className="salesmap" />;
}
