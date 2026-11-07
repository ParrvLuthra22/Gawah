"use client";

import { useEffect, useState } from "react";

const ENGINE_URL = process.env.NEXT_PUBLIC_ENGINE_URL || "http://localhost:8000";

export default function Home() {
  const [health, setHealth] = useState<string>("checking...");

  useEffect(() => {
    fetch(`${ENGINE_URL}/health`)
      .then((r) => r.json())
      .then((data) => setHealth(JSON.stringify(data)))
      .catch(() => setHealth("engine unreachable"));
  }, []);

  return (
    <main className="flex flex-1 flex-col items-center justify-center gap-4 bg-neutral-950 text-neutral-100">
      <h1 className="text-5xl font-semibold tracking-tight">Gawah</h1>
      <p className="text-sm text-neutral-400">
        Engine ({ENGINE_URL}/health):{" "}
        <code className="rounded bg-neutral-900 px-2 py-1 text-neutral-200">{health}</code>
      </p>
    </main>
  );
}
