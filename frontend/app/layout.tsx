import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Ops Assistant: customer support chat", template: "%s · Ops Assistant" },
  description:
    "Chat with the Ops Assistant about your orders, refunds and support tickets. The assistant proposes; the shop's backend decides.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    // Browser extensions (e.g. ones that add webcrx attributes) modify <html> before React loads;
    // this silences the resulting hydration warning for that one element only.
    <html lang="en" suppressHydrationWarning>
      <body className="min-h-screen bg-slate-50 text-slate-900 antialiased">{children}</body>
    </html>
  );
}
