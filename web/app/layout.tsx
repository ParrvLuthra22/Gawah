import type { Metadata } from "next";
import { Shell } from "@/components/shell";
import "./globals.css";

export const metadata: Metadata = {
  title: "GAWAH | Dispute operations",
  description: "Review and synchronize PayPal Sandbox disputes.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full"><Shell>{children}</Shell></body>
    </html>
  );
}
