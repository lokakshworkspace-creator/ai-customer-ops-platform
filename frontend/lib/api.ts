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

export type ChatResponse = {
  conversation_id: string;
  workflow_id: string;
  message_id: string;
  status: "completed" | "waiting_approval" | "escalated" | "failed" | "running";
  reply: string;
  tool_results: ToolResult[];
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
  llm: "fake" | "gemini" | null;
  waiting_for_approval: boolean;
};

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
