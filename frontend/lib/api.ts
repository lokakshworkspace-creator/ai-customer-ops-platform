// Thin, typed client for the backend API. No business logic: it sends what the user typed and
// returns what the server said. Every decision (allowed, denied, needs approval) is the backend's.

export type ToolResult = {
  tool: string;
  status: string;
  decision: string | null;
  reason_code: string | null;
  summary: string;
  tool_call_id: string;
};

// A help article the answer was based on. Computed by the backend from what the search returned,
// never taken from the model's wording.
export type Source = {
  document_id: string;
  title: string;
  heading: string | null;
};

export type ChatResponse = {
  conversation_id: string;
  workflow_id: string;
  message_id: string;
  status: "completed" | "waiting_approval" | "escalated" | "failed" | "running";
  reply: string;
  tool_results: ToolResult[];
  sources: Source[];
  llm: "fake" | "gemini";
};

export type WorkflowView = {
  workflow_id: string;
  conversation_id: string | null;
  status: ChatResponse["status"];
  state: string;
  reply: string | null;
  latest_message: string | null;
  tool_results: ToolResult[];
  sources: Source[];
  llm: "fake" | "gemini" | null;
  waiting_for_approval: boolean;
  // What a still-waiting turn waits for, computed by the backend from the approval and refund:
  // a human decision, or the payout of an approved refund. The page only displays it.
  waiting_for: "approval" | "payout" | null;
};

// --- the signed-in user, and the admin page's data (all authority is the backend's) -------------

export type Me = { id: string; email: string; role: "customer" | "admin" };

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired";

export type ApprovalItem = {
  id: string;
  refund_id: string | null;
  status: ApprovalStatus;
  requested_by: string;
  created_at: string;
  decided_by: string | null;
  decided_at: string | null;
  decision_reason: string | null;
  expires_at: string | null;
  refund: {
    amount: string;
    currency: string;
    reason_code: string;
    status: string;
    order_id: string;
  } | null;
  order: { status: string; total_amount: string; currency: string; placed_at: string } | null;
  review_reasons: string[];
  review_text: string | null;
  requester_refunds_in_window: number | null;
  velocity_window_days: number;
};

export type ApprovalList = { items: ApprovalItem[]; total: number; limit: number; offset: number };

export type KbResult = {
  title: string;
  audience: "customer" | "internal";
  heading: string | null;
  content: string;
  score: number | null;
};

export type KbSearch = { found: boolean; results: KbResult[] };

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public code?: string,
  ) {
    super(message);
  }
}

export const ABORTED = "ABORTED";

async function request<T>(
  path: string,
  init: RequestInit,
  token?: string,
): Promise<{ data: T; replayed: boolean }> {
  let res: Response;
  try {
    res = await fetch(path, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
    });
  } catch {
    if (init.signal?.aborted) throw new ApiError(0, "Request cancelled", ABORTED);
    throw new ApiError(0, "Could not reach the server. Check your connection and try again.");
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof body.detail === "string" ? body.detail : `Request failed (${res.status})`;
    throw new ApiError(res.status, detail, body.code);
  }
  return { data: body as T, replayed: res.headers.get("Idempotent-Replayed") === "true" };
}

// Revokes this token on the server (it gets 401 everywhere afterwards). The page signs out locally
// whatever the answer is.
export async function logoutRequest(token: string): Promise<void> {
  await request<unknown>("/api/v1/auth/logout", { method: "POST" }, token);
}

export async function login(email: string, password: string): Promise<string> {
  const { data } = await request<{ access_token: string }>("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  return data.access_token;
}

export async function sendChat(
  token: string,
  body: { message: string; message_id: string; conversation_id?: string },
  signal?: AbortSignal,
): Promise<ChatResponse> {
  const { data } = await request<ChatResponse>(
    "/api/v1/chat",
    { method: "POST", body: JSON.stringify(body), signal },
    token,
  );
  return data;
}

export async function getWorkflow(
  token: string,
  id: string,
  signal?: AbortSignal,
): Promise<WorkflowView> {
  const { data } = await request<WorkflowView>(
    `/api/v1/workflows/${id}`,
    { method: "GET", signal },
    token,
  );
  return data;
}

export async function getMe(token: string, signal?: AbortSignal): Promise<Me> {
  const { data } = await request<Me>("/api/v1/auth/me", { method: "GET", signal }, token);
  return data;
}

export async function listApprovals(
  token: string,
  status: ApprovalStatus,
  signal?: AbortSignal,
): Promise<ApprovalList> {
  const { data } = await request<ApprovalList>(
    `/api/v1/approvals?status=${status}&limit=50`,
    { method: "GET", signal },
    token,
  );
  return data;
}

// Approve or reject through the EXISTING endpoints. Every refusal (self-approval, wrong role,
// already decided, ...) comes back as the backend's own message and is shown as it is.
export async function decideApproval(
  token: string,
  id: string,
  verb: "approve" | "reject",
  note: string,
): Promise<void> {
  await request<unknown>(
    `/api/v1/approvals/${id}/${verb}`,
    { method: "POST", body: JSON.stringify(note.trim() ? { note: note.trim() } : {}) },
    token,
  );
}

export async function searchKnowledge(
  token: string,
  q: string,
  signal?: AbortSignal,
): Promise<KbSearch> {
  const { data } = await request<KbSearch>(
    `/api/v1/admin/knowledge/search?q=${encodeURIComponent(q)}`,
    { method: "GET", signal },
    token,
  );
  return data;
}
