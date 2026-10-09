"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ABORTED,
  ApiError,
  ChatResponse,
  Source,
  ToolResult,
  getWorkflow,
  login,
  sendChat,
} from "../lib/api";

// This page contains NO business logic. It sends what the user typed and shows what the backend
// returned: the reply, the tool outcomes (decided by the backend), and the waiting-for-approval
// state. Whether something is allowed, denied or needs a human is never decided here.

const TOKEN_KEY = "ops.token";
const POLL_MS = 5000;

// sessionStorage can throw (private windows, blocked site data): the page must still work.
const tokenStore = {
  get(): string | null {
    try {
      return sessionStorage.getItem(TOKEN_KEY);
    } catch {
      return null;
    }
  },
  set(value: string): void {
    try {
      sessionStorage.setItem(TOKEN_KEY, value);
    } catch {
      /* the session just will not survive a reload */
    }
  },
  clear(): void {
    try {
      sessionStorage.removeItem(TOKEN_KEY);
    } catch {
      /* nothing to clear */
    }
  },
};

const messageOf = (err: unknown, fallback: string): string =>
  err instanceof Error && err.message ? err.message : fallback;
const isAborted = (err: unknown): boolean => err instanceof ApiError && err.code === ABORTED;

type Turn = {
  key: string; // the client-generated message_id; resending it is safe (the server replays)
  text: string;
  state: "sending" | "done" | "error";
  error?: string;
  response?: ChatResponse;
  resolved?: { status: string; message: string | null }; // set when a waiting turn is decided
};

const STATUS_STYLE: Record<string, string> = {
  succeeded: "bg-emerald-100 text-emerald-800",
  awaiting_approval: "bg-amber-100 text-amber-800",
  denied: "bg-rose-100 text-rose-800",
  invalid: "bg-rose-100 text-rose-800",
  failed: "bg-rose-100 text-rose-800",
  pending: "bg-slate-100 text-slate-700",
};

function ToolChip({ r }: { r: ToolResult }) {
  return (
    <li className="flex flex-wrap items-center gap-2 rounded border border-slate-200 bg-white px-2 py-1 text-xs">
      <span className="font-mono font-semibold">{r.tool}</span>
      <span className={`rounded px-1.5 py-0.5 ${STATUS_STYLE[r.status] ?? STATUS_STYLE.pending}`}>
        {r.status}
      </span>
      {r.reason_code && <span className="font-mono text-slate-500">{r.reason_code}</span>}
      <span className="text-slate-700">{r.summary}</span>
    </li>
  );
}

// The articles behind an answer, as the backend reported them. Display only.
function SourceChips({ sources }: { sources: Source[] }) {
  if (sources.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-xs" aria-label="Sources">
      <span className="text-slate-500">Sources:</span>
      {sources.map((s) => (
        <span
          key={s.document_id}
          title={s.heading ?? s.title}
          className="rounded-full border border-sky-200 bg-sky-50 px-2 py-0.5 text-sky-900"
        >
          {s.title}
        </span>
      ))}
    </div>
  );
}

function LlmBadge({ llm }: { llm: "fake" | "gemini" }) {
  return (
    <span
      title={llm === "fake" ? "Reply from the built-in fake model" : "Reply from the Gemini model"}
      className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${
        llm === "fake" ? "bg-amber-100 text-amber-800" : "bg-indigo-100 text-indigo-800"
      }`}
    >
      llm: {llm}
    </span>
  );
}

export default function Page() {
  const [token, setToken] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [conversationId, setConversationId] = useState<string | undefined>();
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [pollNote, setPollNote] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const mounted = useRef(true);
  const sending = useRef<AbortController | null>(null);

  // EVERY effect below returns nothing or a cleanup FUNCTION. React calls whatever an effect
  // returns as its cleanup, so an arrow with an expression body (`() => thing()`) is a bug
  // whenever `thing()` returns something: a browser's (or polyfill's) scrollIntoView can return
  // a Promise, which React then fails to call ("destroy is not a function").
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      sending.current?.abort(); // a reply that arrives after the page is gone is ignored
    };
  }, []);

  useEffect(() => {
    setToken(tokenStore.get());
    setReady(true);
  }, []);

  useEffect(() => {
    bottom.current?.scrollIntoView?.({ behavior: "smooth" });
  }, [turns]);

  const logout = useCallback(() => {
    tokenStore.clear();
    setToken(null);
    setTurns([]);
    setConversationId(undefined);
  }, []);

  async function onLogin(e: React.FormEvent) {
    e.preventDefault();
    setLoginError(null);
    try {
      const t = await login(email, password);
      if (!mounted.current) return;
      tokenStore.set(t);
      setToken(t);
      setPassword("");
    } catch (err) {
      if (mounted.current) setLoginError(messageOf(err, "Login failed"));
    }
  }

  const update = (key: string, patch: Partial<Turn>) =>
    setTurns((ts) => ts.map((t) => (t.key === key ? { ...t, ...patch } : t)));

  async function deliver(turn: Turn) {
    if (!token) return;
    const ctrl = new AbortController();
    sending.current = ctrl;
    setBusy(true);
    update(turn.key, { state: "sending", error: undefined });
    try {
      const response = await sendChat(
        token,
        {
          message: turn.text,
          message_id: turn.key,
          ...(conversationId ? { conversation_id: conversationId } : {}),
        },
        ctrl.signal,
      );
      if (!mounted.current) return;
      setConversationId(response.conversation_id);
      update(turn.key, { state: "done", response });
    } catch (err) {
      if (!mounted.current || isAborted(err)) return;
      if (err instanceof ApiError && err.status === 401) return logout();
      update(turn.key, { state: "error", error: messageOf(err, "Something went wrong") });
    } finally {
      // however the request ended, the page must never stay stuck in "sending"
      if (sending.current === ctrl) sending.current = null;
      if (mounted.current) setBusy(false);
    }
  }

  function onSend(e: React.FormEvent) {
    e.preventDefault();
    const text = draft.trim();
    if (!text || busy) return;
    const turn: Turn = { key: crypto.randomUUID(), text, state: "sending" };
    setTurns((ts) => [...ts, turn]);
    setDraft("");
    void deliver(turn);
  }

  // While a refund waits for a human, ask the backend now and then. The backend decides when it
  // is resolved; this only displays the answer.
  const waiting = turns.filter(
    (t) => t.response?.status === "waiting_approval" && !t.resolved,
  );
  // The effect reads the CURRENT list through a ref, so the timer is only recreated when the set
  // of waiting turns (or the login) changes, not on every render.
  const waitingRef = useRef(waiting);
  const waitingKeys = waiting.map((t) => t.key).join(",");
  useEffect(() => {
    waitingRef.current = waiting;
  });
  useEffect(() => {
    if (!token || !waitingKeys) return;
    const ctrl = new AbortController();
    let cancelled = false;
    let running = false; // never two polls at once if the server is slow

    async function poll() {
      if (running || cancelled || !token) return;
      running = true;
      try {
        for (const t of waitingRef.current) {
          if (cancelled || !t.response) return;
          const w = await getWorkflow(token, t.response.workflow_id, ctrl.signal);
          if (cancelled) return;
          setPollNote(null);
          if (!w.waiting_for_approval) {
            update(t.key, { resolved: { status: w.status, message: w.latest_message } });
          }
        }
      } catch (err) {
        if (cancelled || isAborted(err)) return;
        if (err instanceof ApiError && err.status === 401) return logout();
        // a failed poll is shown calmly and retried on the next tick; it never breaks the page
        setPollNote(`Could not check the approval status (${messageOf(err, "error")}). Retrying…`);
      } finally {
        running = false;
      }
    }

    const id = setInterval(() => {
      void poll();
    }, POLL_MS);
    return () => {
      cancelled = true;
      ctrl.abort();
      clearInterval(id);
    };
  }, [token, waitingKeys, logout]);

  if (!ready) return null;

  if (!token) {
    return (
      <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center gap-4 p-6">
        <h1 className="text-xl font-semibold">Ops Assistant</h1>
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

  const llm = turns.find((t) => t.response)?.response?.llm;

  return (
    <main className="mx-auto flex h-screen max-w-2xl flex-col p-4">
      <header className="flex items-center justify-between pb-3">
        <h1 className="text-lg font-semibold">Ops Assistant</h1>
        <div className="flex items-center gap-3 text-sm">
          {llm && <LlmBadge llm={llm} />}
          <button
            className="text-slate-600 underline"
            onClick={() => {
              setTurns([]);
              setConversationId(undefined);
            }}
          >
            New conversation
          </button>
          <button className="text-slate-600 underline" onClick={logout}>
            Sign out
          </button>
        </div>
      </header>

      <div className="flex-1 space-y-4 overflow-y-auto rounded border border-slate-200 bg-white p-3">
        {turns.length === 0 && (
          <p className="text-sm text-slate-500">
            Try: “my order arrived damaged, refund me” or “how long does shipping take?”. The
            assistant proposes; the shop’s backend decides.
          </p>
        )}
        {turns.map((t) => (
          <div key={t.key} className="space-y-2">
            <div className="ml-auto w-fit max-w-[85%] rounded-lg bg-slate-900 px-3 py-2 text-sm text-white">
              {t.text}
            </div>
            {t.state === "sending" && <p className="text-sm text-slate-500">Thinking…</p>}
            {t.state === "error" && (
              <div className="flex items-center gap-3 rounded border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800">
                <span>{t.error}</span>
                {/* the SAME message_id is sent again: the server never repeats an action */}
                <button className="underline" disabled={busy} onClick={() => void deliver(t)}>
                  Retry
                </button>
              </div>
            )}
            {t.response && (
              <div className="max-w-[90%] space-y-2">
                <div className="flex items-center gap-2">
                  <LlmBadge llm={t.response.llm} />
                  {t.response.status !== "completed" && (
                    <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] uppercase text-slate-600">
                      {t.resolved?.status ?? t.response.status}
                    </span>
                  )}
                </div>
                <div className="whitespace-pre-wrap rounded-lg bg-slate-100 px-3 py-2 text-sm">
                  {t.response.reply}
                </div>
                <SourceChips sources={t.response.sources ?? []} />
                {t.response.tool_results.length > 0 && (
                  <ul className="space-y-1">
                    {t.response.tool_results.map((r) => (
                      <ToolChip key={r.tool_call_id} r={r} />
                    ))}
                  </ul>
                )}
                {t.response.status === "waiting_approval" && !t.resolved && (
                  <p className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                    ⏳ Waiting for human approval. This page checks every few seconds.
                    {pollNote && <span className="mt-1 block text-xs">{pollNote}</span>}
                  </p>
                )}
                {t.resolved?.message && (
                  <p className="rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-900">
                    {t.resolved.message}
                  </p>
                )}
              </div>
            )}
          </div>
        ))}
        <div ref={bottom} />
      </div>

      <form onSubmit={onSend} className="flex gap-2 pt-3">
        <input
          className="flex-1 rounded border border-slate-300 px-3 py-2"
          placeholder="Type a message…"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          maxLength={2000}
          autoFocus
        />
        <button
          className="rounded bg-slate-900 px-4 py-2 font-medium text-white disabled:opacity-50"
          disabled={busy || !draft.trim()}
        >
          Send
        </button>
      </form>
    </main>
  );
}
