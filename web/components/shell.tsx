"use client";

import { CircleHelp, Cable, FolderOpen, ListChecks, Scale, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Suspense, useState, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const navigation = [
  { href: "/disputes", label: "Disputes", icon: ListChecks },
  { href: "/evidence", label: "Evidence", icon: FolderOpen },
  { href: "/counsel", label: "AI Counsel", icon: Scale },
  { href: "/integrations/paypal", label: "PayPal integration", icon: Cable },
];

function Navigation({ pathname }: { pathname?: string }) {
  return (
    <nav className="main-nav" aria-label="Workspace navigation">
      <div className="nav-label">WORKSPACE</div>
      {navigation.map(({ href, label, icon: Icon }) => {
        const active = pathname === href || pathname?.startsWith(href + "/");
        return (
          <Link key={href} href={href} aria-current={active ? "page" : undefined}>
            <Icon size={18} aria-hidden="true" /> {label}
          </Link>
        );
      })}
    </nav>
  );
}

function ActiveNavigation() {
  return <Navigation pathname={usePathname()} />;
}

export function Shell({ children }: { children: ReactNode }) {
  const [queryClient] = useState(() => new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 30_000, refetchOnWindowFocus: false } },
  }));
  return (
    <QueryClientProvider client={queryClient}>
      <div className="workspace">
        <aside className="sidebar">
          <Link href="/disputes" className="brand">
            <span className="brand-icon"><ShieldCheck size={23} aria-hidden="true" /></span>
            <span>GAWAH<small>DISPUTE COUNSEL</small></span>
          </Link>
          <Suspense fallback={<Navigation />}><ActiveNavigation /></Suspense>
          <div className="sidebar-note"><CircleHelp size={16} aria-hidden="true" /> Sandbox workspace</div>
        </aside>
        <div className="workspace-body">
          <header className="topbar">
            <span>Dispute operations</span>
            <span className="sandbox-badge"><span /> Sandbox</span>
          </header>
          <main className="content">{children}</main>
        </div>
      </div>
    </QueryClientProvider>
  );
}
