import type { Metadata } from "next";

// app/admin/page.tsx is a client component and cannot export metadata, so the /admin title and
// description live here. The page is for staff: keep it out of search engines.
export const metadata: Metadata = {
  title: "Admin approvals",
  description: "Review the refund requests that need a person, and approve or reject them.",
  robots: { index: false, follow: false },
};

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  return children;
}
