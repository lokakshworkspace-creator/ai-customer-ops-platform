"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  ApprovalItem,
  ApprovalList,
  ApprovalStatus,
  KbSearch,
  Me,
  decideApproval,
  getMe,
  listApprovals,
  login,
  searchKnowledge,
} from "../../lib/api";
import { isAborted, messageOf, tokenStore } from "../../lib/auth";

// The admin page contains NO business logic. It lists what the backend returns and sends the
// administrator's click to the EXISTING approve / reject endpoints. Whether that is allowed
// (wrong role, approving your own request, already decided, ...) is decided by the backend and its
// answer is shown as it is. The role from /auth/me only chooses what to render: the API itself
// answers 403 to anyone who is not an administrator, whatever this page does.

const REFRESH_MS = 15000;
const TABS: ApprovalStatus[] = ["pending", "approved", "rejected", "expired"];

const short = (id: string | null): string => (id ? id.slice(0, 8) : "-");
const when = (iso: string | null): string => (iso ? new Date(iso).toLocaleString() : "-");

type Msg = { kind: "ok" | "error"; text: string };

const errorText = (err: unknown): string =>
  err instanceof ApiError
    ? `${err.message}${err.code ? ` (${err.code})` : ""}`
    : messageOf(err, "Something went wrong");

const STATUS_STYLE: Record<string, string> = {
  pending: "bg-amber-100 text-amber-800",
  approved: "bg-emerald-100 text-emerald-800",
  rejected: "bg-rose-100 text-rose-800",
  expired: "bg-slate-200 text-slate-700",
};

function ApprovalCard(props: {
  a: ApprovalItem;
  note: string;
  busy: boolean;
  msg?: Msg;
  onNote: (v: string) => void;
  onDecide: (verb: "approve" | "reject") => void;
}) {
  const { a, note, busy, msg, onNote, onDecide } = props;
  return (
    <li className="space-y-2 rounded border border-slate-200 bg-white p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-base font-semibold">
          {a.refund ? `${a.refund.amount} ${a.refund.currency}` : "(no refund)"}
        </span>
        {a.refund && (
          <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs">
            {a.refund.reason_code}
          </span>
        )}
        <span className={`rounded px-1.5 py-0.5 text-xs ${STATUS_STYLE[a.status] ?? ""}`}>
          {a.status}
        </span>
        {a.refund && (
          <span className="text-xs text-slate-500">refund is {a.refund.status}</span>
        )}
        <span className="ml-auto text-xs text-slate-500">requested {when(a.created_at)}</span>
      </div>

      <div className="space-y-1">
        <div className="text-xs font-medium text-slate-600">Why it needs a person</div>
        <div className="flex flex-wrap gap-1">
          {a.review_reasons.map((r) => (
            <span key={r} className="rounded bg-amber-50 px-1.5 py-0.5 font-mono text-xs text-amber-900">
              {r}
            </span>
          ))}
        </div>
        {a.review_text && <p className="text-slate-700">{a.review_text}</p>}
      </div>

      <div className="grid gap-1 text-xs text-slate-600 sm:grid-cols-2">
        <div>
          Order <span className="font-mono">{short(a.refund?.order_id ?? null)}</span>
          {a.order &&
            ` · ${a.order.status} · total ${a.order.total_amount} ${a.order.currency} · placed ${when(a.order.placed_at)}`}
        </div>
        <div>
          Requester <span className="font-mono">{short(a.requested_by)}</span>
          {a.requester_refunds_in_window !== null &&
            ` · ${a.requester_refunds_in_window} refund(s) in the last ${a.velocity_window_days} days`}
        </div>
      </div>

      {a.status !== "pending" && (
        <div className="rounded bg-slate-50 px-2 py-1 text-xs text-slate-700">
          Decided by <span className="font-mono">{short(a.decided_by)}</span> on {when(a.decided_at)}
          {a.decision_reason && <> — “{a.decision_reason}”</>}
        </div>
      )}

      {a.status === "pending" && (
        <div className="flex flex-wrap items-center gap-2">
          <input
            className="min-w-48 flex-1 rounded border border-slate-300 px-2 py-1"
            placeholder="Note (optional)"
            value={note}
            maxLength={500}
            onChange={(e) => onNote(e.target.value)}
            aria-label="Decision note"
          />
          <button
            className="rounded bg-emerald-700 px-3 py-1 font-medium text-white disabled:opacity-50"
            disabled={busy}
            onClick={() => onDecide("approve")}
          >
            Approve
          </button>
          <button
            className="rounded bg-rose-700 px-3 py-1 font-medium text-white disabled:opacity-50"
            disabled={busy}
            onClick={() => onDecide("reject")}
          >
            Reject
          </button>
        </div>
      )}

      {msg && (
        <p
          role={msg.kind === "error" ? "alert" : "status"}
          className={`rounded px-2 py-1 text-xs ${
            msg.kind === "error" ? "bg-rose-50 text-rose-800" : "bg-emerald-50 text-emerald-800"
          }`}
        >
          {msg.text}
        </p>
      )}
    </li>
  );
}

export default function AdminPage() {
  const [token, setToken] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loginError, setLoginError] = useState<string | null>(null);
  const [me, setMe] = useState<Me | null>(null);
  const [meError, setMeError] = useState<string | null>(null);
  const [tab, setTab] = useState<ApprovalStatus>("pending");
  const [list, setList] = useState<ApprovalList | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [msgs, setMsgs] = useState<Record<string, Msg>>({});
  const [busyId, setBusyId] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [kb, setKb] = useState<KbSearch | null>(null);
  const [kbError, setKbError] = useState<string | null>(null);
  const [kbBusy, setKbBusy] = useState(false);

  useEffect(() => {
    setToken(tokenStore.get());
    setReady(true);
  }, []);

  const logout = useCallback(() => {
    tokenStore.clear();
    setToken(null);
    setMe(null);
    setList(null);
    setKb(null);
  }, []);

  // Who is signed in, from the database (not from the token).
  useEffect(() => {
    if (!token) return;
    const ctrl = new AbortController();
    setMeError(null);
    getMe(token, ctrl.signal)
      .then(setMe)
      .catch((err) => {
        if (isAborted(err)) return;
        if (err instanceof ApiError && err.status === 401) return logout();
        setMeError(messageOf(err, "Could not load your account"));
      });
    return () => ctrl.abort();
  }, [token, logout]);

  const isAdmin = me?.role === "admin";

  const load = useCallback(
    async (signal?: AbortSignal) => {
      if (!token) return;
      try {
        const l = await listApprovals(token, tab, signal);
        setList(l);
        setLoadError(null);
      } catch (err) {
        if (isAborted(err)) return;
        if (err instanceof ApiError && err.status === 401) return logout();
        setLoadError(errorText(err));
      }
    },
    [token, tab, logout],
  );

  // Only an administrator's page asks for approvals at all; everyone else asks for nothing.
  useEffect(() => {
    if (!token || !isAdmin) return;
    const ctrl = new AbortController();
    void load(ctrl.signal);
    const id = setInterval(() => {
      void load(ctrl.signal);
    }, REFRESH_MS);
    return () => {
      ctrl.abort();
      clearInterval(id);
    };
  }, [token, isAdmin, load]);

  async function onLogin(e: React.FormEvent) {
    e.preventDefault();
    setLoginError(null);
    try {
      const t = await login(email, password);
      tokenStore.set(t);
      setToken(t);
      setPassword("");
    } catch (err) {
      setLoginError(messageOf(err, "Login failed"));
    }
  }

  async function onDecide(a: ApprovalItem, verb: "approve" | "reject") {
    if (!token) return;
    setBusyId(a.id);
    setMsgs((m) => {
      const { [a.id]: _gone, ...rest } = m;
      return rest;
    });
    try {
      await decideApproval(token, a.id, verb, notes[a.id] ?? "");
      setMsgs((m) => ({
        ...m,
        [a.id]: { kind: "ok", text: verb === "approve" ? "Approved." : "Rejected." },
      }));
      await load();
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return logout();
      // the backend's own refusal, shown as it is (SELF_APPROVAL, ALREADY_DECIDED, 403, ...)
      setMsgs((m) => ({ ...m, [a.id]: { kind: "error", text: errorText(err) } }));
    } finally {
      setBusyId(null);
    }
  }

  async function onSearch(e: React.FormEvent) {
    e.preventDefault();
    if (!token || q.trim().length < 2) return;
    setKbBusy(true);
    setKbError(null);
    try {
      setKb(await searchKnowledge(token, q.trim()));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return logout();
      setKb(null);
      setKbError(errorText(err));
    } finally {
      setKbBusy(false);
    }
  }

  if (!ready) return null;

  if (!token) {
    return (
      <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center gap-4 p-6">
        <h1 className="text-xl font-semibold">Ops Admin</h1>
        <form onSubmit={onLogin} className="flex flex-col gap-3">
          <input
            className="rounded border border-slate-300 px-3 py-2"
            placeholder="Email"
            type="email"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
          <input
            className="rounded border border-slate-300 px-3 py-2"
            placeholder="Password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
          {loginError && <p className="text-sm text-rose-700">{loginError}</p>}
          <button className="rounded bg-slate-900 px-3 py-2 font-medium text-white">Sign in</button>
        </form>
      </main>
    );
  }

  const header = (
    <header className="flex flex-wrap items-center justify-between gap-2 pb-3">
      <h1 className="text-lg font-semibold">Ops Admin</h1>
      <div className="flex items-center gap-3 text-sm">
        {me && (
          <span data-testid="whoami" className="text-slate-700">
            Signed in as <strong>{me.email}</strong> ({me.role})
          </span>
        )}
        <button className="text-slate-600 underline" onClick={logout}>
          Sign out
        </button>
      </div>
    </header>
  );

  if (!me) {
    return (
      <main className="mx-auto max-w-3xl p-4">
        {header}
        <p className="text-sm text-slate-500">{meError ?? "Loading…"}</p>
      </main>
    );
  }

  if (!isAdmin) {
    // Nothing is requested for a non-administrator. (The API would answer 403 anyway.)
    return (
      <main className="mx-auto max-w-3xl p-4">
        {header}
        <p role="alert" className="rounded border border-slate-200 bg-white p-4 text-sm">
          You do not have access to this page.
        </p>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-3xl space-y-4 p-4">
      {header}

      <nav className="flex gap-1 border-b border-slate-200" aria-label="Approval status">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => {
              setList(null);
              setTab(t);
            }}
            className={`px-3 py-1.5 text-sm capitalize ${
              tab === t ? "border-b-2 border-slate-900 font-semibold" : "text-slate-600"
            }`}
          >
            {t}
          </button>
        ))}
        <button className="ml-auto px-3 py-1.5 text-sm text-slate-600 underline" onClick={() => void load()}>
          Refresh
        </button>
      </nav>

      {loadError && (
        <p role="alert" className="rounded bg-rose-50 px-3 py-2 text-sm text-rose-800">
          {loadError}
        </p>
      )}
      {!list && !loadError && <p className="text-sm text-slate-500">Loading…</p>}
      {list && list.items.length === 0 && (
        <p className="text-sm text-slate-500">Nothing {tab === "pending" ? "is waiting" : tab}.</p>
      )}
      {list && list.items.length > 0 && (
        <ul className="space-y-3" data-testid="approvals">
          {list.items.map((a) => (
            <ApprovalCard
              key={a.id}
              a={a}
              note={notes[a.id] ?? ""}
              busy={busyId === a.id}
              msg={msgs[a.id]}
              onNote={(v) => setNotes((n) => ({ ...n, [a.id]: v }))}
              onDecide={(verb) => void onDecide(a, verb)}
            />
          ))}
          {list.total > list.items.length && (
            <li className="text-xs text-slate-500">
              Showing {list.items.length} of {list.total}.
            </li>
          )}
        </ul>
      )}

      <section className="space-y-2 rounded border border-slate-200 bg-white p-3">
        <h2 className="text-sm font-semibold">Policy lookup (staff, includes internal documents)</h2>
        <form onSubmit={onSearch} className="flex gap-2">
          <input
            className="flex-1 rounded border border-slate-300 px-2 py-1 text-sm"
            placeholder="Search the knowledge base…"
            value={q}
            maxLength={500}
            onChange={(e) => setQ(e.target.value)}
            aria-label="Policy lookup"
          />
          <button
            className="rounded bg-slate-900 px-3 py-1 text-sm font-medium text-white disabled:opacity-50"
            disabled={kbBusy || q.trim().length < 2}
          >
            Search
          </button>
        </form>
        {kbError && (
          <p role="alert" className="text-xs text-rose-800">
            {kbError}
          </p>
        )}
        {kb && !kb.found && <p className="text-xs text-slate-500">No relevant article found.</p>}
        {kb && kb.results.length > 0 && (
          <ul className="space-y-2">
            {kb.results.map((r, i) => (
              <li key={i} className="rounded border border-slate-200 p-2 text-xs">
                <div className="flex items-center gap-2">
                  <span className="font-semibold">{r.title}</span>
                  <span
                    className={`rounded px-1.5 py-0.5 ${
                      r.audience === "internal"
                        ? "bg-violet-100 text-violet-800"
                        : "bg-sky-100 text-sky-800"
                    }`}
                  >
                    {r.audience}
                  </span>
                  {r.heading && <span className="text-slate-500">{r.heading}</span>}
                </div>
                <p className="mt-1 whitespace-pre-wrap text-slate-700">{r.content}</p>
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}
