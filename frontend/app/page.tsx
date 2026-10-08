"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  ChatResponse,
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

function LlmBadge({ llm }: { llm: "fake" | "claude" }) {
  return (
    <span
      title={llm === "fake" ? "Reply from the built-in fake model" : "Reply from the Claude model"}
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
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setToken(sessionStorage.getItem(TOKEN_KEY));
    setReady(true);
  }, []);
  useEffect(() => bottom.current?.scrollIntoView({ behavior: "smooth" }), [turns]);

  const logout = useCallback(() => {
    sessionStorage.removeItem(TOKEN_KEY);
    setToken(null);
    setTurns([]);
    setConversationId(undefined);
  }, []);

  async function onLogin(e: React.FormEvent) {
    e.preventDefault();
    setLoginError(null);
    try {
      const t = await login(email, password);
      sessionStorage.setItem(TOKEN_KEY, t);
      setToken(t);
      setPassword("");
    } catch (err) {
      setLoginError(err instanceof ApiError ? err.message : "Login failed");
    }
  }

  const update = (key: string, patch: Partial<Turn>) =>
    setTurns((ts) => ts.map((t) => (t.key === key ? { ...t, ...patch } : t)));

  async function deliver(turn: Turn) {
    if (!token) return;
    setBusy(true);
    update(turn.key, { state: "sending", error: undefined });
    try {
      const response = await sendChat(token, {
        message: turn.text,
        message_id: turn.key,
        ...(conversationId ? { conversation_id: conversationId } : {}),
      });
      setConversationId(response.conversation_id);
      update(turn.key, { state: "done", response });
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return logout();
      update(turn.key, {
        state: "error",
        error: err instanceof ApiError ? err.message : "Something went wrong",
      });
    } finally {
      setBusy(false);
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
  useEffect(() => {
    if (!token || waiting.length === 0) return;
    const id = setInterval(async () => {
      for (const t of waiting) {
        try {
          const w = await getWorkflow(token, t.response!.workflow_id);
          if (!w.waiting_for_approval) {
            update(t.key, { resolved: { status: w.status, message: w.latest_message } });
          }
        } catch (err) {
          if (err instanceof ApiError && err.status === 401) logout();
        }
      }
    }, POLL_MS);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, waiting.map((t) => t.key).join(",")]);

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
            Try: “my order arrived damaged, refund me”. The assistant proposes; the shop’s backend
            decides.
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
