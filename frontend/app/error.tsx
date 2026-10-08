"use client";

// A render error anywhere under the layout lands here instead of blanking the page.
export default function ErrorBoundary({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center gap-4 p-6">
      <h1 className="text-xl font-semibold">Something went wrong</h1>
      <p className="text-sm text-slate-600">
        The page hit an unexpected error. Nothing you sent was lost on the server: messages are
        safe to send again.
      </p>
      {error.message && (
        <p className="rounded border border-slate-200 bg-white px-3 py-2 font-mono text-xs text-slate-600">
          {error.message}
        </p>
      )}
      <button className="rounded bg-slate-900 px-3 py-2 font-medium text-white" onClick={reset}>
        Try again
      </button>
    </main>
  );
}
