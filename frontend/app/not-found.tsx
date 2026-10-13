import Link from "next/link";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Page not found",
  robots: { index: false, follow: false },
};

// Shown (with a 404 status) for any address that is not one of the two pages.
export default function NotFound() {
  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center gap-4 p-6">
      <h1 className="text-xl font-semibold">Page not found</h1>
      <p className="text-sm text-slate-600">
        There is nothing at this address. This app has two pages.
      </p>
      <nav className="flex gap-4 text-sm">
        <Link className="underline" href="/">
          Chat assistant
        </Link>
        <Link className="underline" href="/admin">
          Admin approvals
        </Link>
      </nav>
    </main>
  );
}
