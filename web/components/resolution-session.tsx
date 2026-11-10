"use client";

import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { api, humanize } from "@/lib/disputes";
import type { Actor } from "@/lib/resolution";

type Session = {
  actor: Actor;
  cacheKey: string;
  call: <T>(path: string, init?: RequestInit) => Promise<T>;
};
const Context = createContext<Session | null>(null);

export function useResolutionSession() {
  const value = useContext(Context);
  if (!value) throw new Error("Resolution Center session is required.");
  return value;
}

export function ResolutionSession({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<{ actor: Actor; credential: string; cacheKey: string } | null>(null);
  const [credential, setCredential] = useState("");
  const [signingIn, setSigningIn] = useState(false);
  const [error, setError] = useState<string>();
  const queryClient = useQueryClient();
  async function signIn() {
    const value = credential.trim();
    if (!value || signingIn) return;
    setSigningIn(true);
    setError(undefined);
    setCredential("");
    try {
      const actor = await api<Actor>("/resolution/actor", { headers: { Authorization: `Bearer ${value}` } });
      setSession({ actor, credential: value, cacheKey: crypto.randomUUID() });
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Sign-in failed.");
    } finally {
      setSigningIn(false);
    }
  }
  const call = useCallback(<T,>(path: string, init?: RequestInit) => {
    const headers = new Headers(init?.headers);
    headers.set("Authorization", `Bearer ${session?.credential ?? ""}`);
    if (init?.body) headers.set("Content-Type", "application/json");
    return api<T>(`/resolution${path}`, { ...init, headers });
  }, [session]);

  if (!session) return <section className="panel">
    <div className="panel-heading"><h1 className="text-xl font-bold">Resolution Center</h1></div>
    <form className="max-w-xl space-y-4 p-5" onSubmit={event => {
      event.preventDefault();
      void signIn();
    }}>
      <p className="muted">Enter your assigned operator credential to review dispute workflows.</p>
      <label className="block text-sm font-semibold">Operator credential
        <input className="mt-1 block w-full rounded border px-3 py-2" type="password"
          autoComplete="off" required maxLength={194} value={credential} disabled={signingIn}
          onChange={event => setCredential(event.target.value)} />
      </label>
      <Button type="submit" disabled={signingIn}>{signingIn ? "Signing in…" : "Sign in"}</Button>
      {error && <p className="notice red" role="alert">{error}</p>}
    </form>
  </section>;

  return <Context.Provider value={{ actor: session.actor, cacheKey: session.cacheKey, call }}>
    <div className="actions mb-4 justify-end text-sm">
      <span className="muted">{session.actor.id} · {humanize(session.actor.role)}</span>
      <Button variant="outline" onClick={() => {
        setSession(null);
        setError(undefined);
        void queryClient.cancelQueries({ queryKey: ["resolution"] });
        queryClient.removeQueries({ queryKey: ["resolution"] });
      }}>Sign out</Button>
    </div>
    {children}
  </Context.Provider>;
}
