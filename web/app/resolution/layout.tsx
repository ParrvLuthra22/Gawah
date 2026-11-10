import type { ReactNode } from "react";
import { ResolutionSession } from "@/components/resolution-session";

export default function ResolutionLayout({ children }: { children: ReactNode }) {
  return <ResolutionSession>{children}</ResolutionSession>;
}
