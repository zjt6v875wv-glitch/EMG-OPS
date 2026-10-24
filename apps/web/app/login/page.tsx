"use client";
import { useState } from "react";
import { api } from "@/lib/api";

export default function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await api("/auth/login", { method: "POST", json: { email, password } });
      const next = new URLSearchParams(location.search).get("next");
      location.href = next && next.startsWith("/") ? next : "/";
    } catch (err: any) {
      setError(err.message);
    }
  };
  return (
    <form className="panel login" onSubmit={submit}>
      <div className="brand" style={{ padding: 0, color: "var(--text)", marginBottom: 16 }}><span className="mark" style={{ color: "#fff" }}>EMG</span> Operations</div>
      {error && <div className="alert error">{error}</div>}
      <div className="form" style={{ gridTemplateColumns: "1fr" }}>
        <label>Email<input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required /></label>
        <label>Password<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
        <button className="primary" type="submit">Sign in</button>
      </div>
    </form>
  );
}
