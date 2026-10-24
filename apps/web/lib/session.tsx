"use client";
import { createContext, useContext, useEffect, useState } from "react";
import { api } from "./api";

export type User = { id: string; email: string; name: string; role: string; person_name?: string | null };
type Session = { user: User | null; permissions: string[]; loading: boolean; can: (p: string) => boolean; refresh: () => void };

const Ctx = createContext<Session>({ user: null, permissions: [], loading: true, can: () => false, refresh: () => {} });

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<{ user: User | null; permissions: string[]; loading: boolean }>({ user: null, permissions: [], loading: true });
  const load = () =>
    api("/auth/me")
      .then((r) => setState({ user: r.user, permissions: r.permissions, loading: false }))
      .catch(() => setState({ user: null, permissions: [], loading: false }));
  useEffect(() => {
    if (!location.pathname.startsWith("/login")) load();
    else setState((s) => ({ ...s, loading: false }));
  }, []);
  return (
    <Ctx.Provider value={{ ...state, can: (p) => state.permissions.includes(p), refresh: load }}>{children}</Ctx.Provider>
  );
}

export const useSession = () => useContext(Ctx);
