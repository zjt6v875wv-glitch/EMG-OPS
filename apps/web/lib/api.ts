// Thin client for the EMG Ops API. Money arrives as strings and is never parsed into floats for math.
export class ApiError extends Error {
  constructor(public status: number, message: string, public items?: any[]) {
    super(message);
  }
}

export async function api<T = any>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const { json, ...rest } = init;
  const res = await fetch(`/api${path}`, {
    credentials: "include",
    ...rest,
    headers: json !== undefined ? { "Content-Type": "application/json", ...(rest.headers || {}) } : rest.headers,
    body: json !== undefined ? JSON.stringify(json) : rest.body,
  });
  if (res.status === 401 && typeof window !== "undefined" && !location.pathname.startsWith("/login")) {
    location.href = `/login?next=${encodeURIComponent(location.pathname + location.search)}`;
  }
  const ct = res.headers.get("content-type") || "";
  const body = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new ApiError(res.status, (body && body.detail) || res.statusText, body?.items);
  return body as T;
}

export function money(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === "") return "";
  const s = String(v);
  const neg = s.startsWith("-");
  const [i, f = ""] = (neg ? s.slice(1) : s).split(".");
  const cents = (f + "00").slice(0, 2);
  const grouped = i.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${neg ? "-" : ""}$${grouped}.${cents}`;
}

export function isZero(v: string | number | null | undefined): boolean {
  return v === null || v === undefined || Number(v) === 0;
}

export function currentWeek(d = new Date()): string {
  const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const day = t.getUTCDay() || 7;
  t.setUTCDate(t.getUTCDate() + 4 - day);
  const y = t.getUTCFullYear();
  const w = Math.ceil(((t.getTime() - Date.UTC(y, 0, 1)) / 86400000 + 1) / 7);
  return `${y}-W${String(w).padStart(2, "0")}`;
}

export function shortDate(v?: string | null) {
  return v ? new Date(v).toLocaleString() : "";
}
