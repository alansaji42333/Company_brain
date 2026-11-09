// Typed client for the Company Brain /api/v1 endpoints.

export interface Source {
  name: string;
  url: string;
  type: string;
}

export interface ChatResponse {
  type: "message" | "confirmation_required";
  conversation_id?: string;
  answer?: string;
  sources?: Source[];
  description?: string;
  tool_name?: string;
  tool_input?: Record<string, unknown>;
  explanation?: string;
}

export interface ConfirmResponse {
  type: string;
  conversation_id?: string;
  answer?: string;
  sources?: Source[];
  error?: string;
}

export interface SkillSummary {
  id: string;
  title: string;
  status: "draft" | "approved" | "rejected";
  created_at: string;
  updated_at: string;
}

export interface SkillDetail extends SkillSummary {
  source_chunk_ids: string[];
  content: string;
}

export interface IngestResponse {
  status: string;
  files_processed?: number;
  chunks_stored?: number;
  job_id?: string;
}

export interface SynthesisResponse {
  status: string;
  batches_processed: number;
  new_skills: number;
  skipped_duplicates: number;
}

export interface ConversationSummary {
  id: string;
  created_at: string;
  updated_at: string;
}

export interface OrgMember {
  user_id: string;
  role: string;
  created_at: string | null;
}

export interface MyOrg {
  user_id: string;
  id: string;
  name: string;
  slug: string;
  role: string;
  members: OrgMember[];
}

export interface MyOrgSummary {
  id: string;
  name: string;
  slug: string;
  role: string;
}

export interface ToolProposal {
  type: "tool_proposal";
  tool_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  description: string;
  conversation_id: string;
}

export interface ToolResultEvent {
  type: "tool_result";
  tool_id: string;
  approved: boolean;
  success: boolean;
  result: Record<string, unknown>;
}

export interface ChatStreamHandlers {
  onSources?: (sources: Source[], conversationId: string) => void;
  onToken?: (content: string) => void;
  onToolProposal?: (evt: ToolProposal) => void;
  onToolResult?: (evt: ToolResultEvent) => void;
  onMessage?: (resp: ChatResponse) => void;
  onError?: (detail: string) => void;
}

const API_BASE = "/api/v1";

// --- auth plumbing -----------------------------------------------------------
// The access token comes from a registered getter — Auth0's
// `getAccessTokenSilently` in production (no JWT ever stored client-side),
// or the dev paste-token flow (localStorage "auth_token") when the app runs
// without VITE_AUTH0_* config. Fetches attach it as a Bearer token; the
// WebSocket auth message also carries the active-org hint.
type TokenGetter = (forceRefresh?: boolean) => Promise<string>;

let tokenGetter: TokenGetter = async () => localStorage.getItem("auth_token") || "";
let onUnauthorized: (() => void) | null = null;

/**
 * Register the app's auth callbacks (called once on startup, after sign-in).
 * `tokenGetter(force)` must return the access token, refreshing it when
 * stale — `force` asks the underlying provider to bypass any cache.
 * `onUnauthorized` fires when a request still gets 401 after one silent
 * re-auth retry; the app then returns the user to the sign-in screen.
 */
export function registerAuth(options: { tokenGetter: TokenGetter; onUnauthorized?: () => void }): void {
  tokenGetter = options.tokenGetter;
  onUnauthorized = options.onUnauthorized ?? null;
}

// Active org hint (org switcher). Membership is always verified
// server-side; this only selects WHICH verified membership applies.
let activeOrgId = localStorage.getItem("active_org_id") || "";

export function getActiveOrgId(): string {
  return activeOrgId;
}

export function setActiveOrgId(orgId: string | null): void {
  activeOrgId = orgId || "";
  if (activeOrgId) localStorage.setItem("active_org_id", activeOrgId);
  else localStorage.removeItem("active_org_id");
}

function extraHeaders(extra: HeadersInit | undefined): Record<string, string> {
  // All internal callers pass plain objects; Headers/arrays are not used.
  return { ...((extra as Record<string, string>) || {}) };
}

async function requestOnce(path: string, init: RequestInit, tokenValue: string, orgValue: string): Promise<Response> {
  const headers = extraHeaders(init.headers);
  headers.Authorization = `Bearer ${tokenValue}`;
  if (orgValue) headers["X-Org-Id"] = orgValue;
  return fetch(`${API_BASE}${path}`, { ...init, headers });
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res = await requestOnce(path, init, await tokenGetter(), activeOrgId);
  if (res.status === 401) {
    // One silent re-auth retry with a forcibly refreshed token. If that
    // still gets a 401 the session is unusable — hand the user to the
    // sign-in screen.
    let refreshed = false;
    try {
      await tokenGetter(true);
      res = await requestOnce(path, init, await tokenGetter(), activeOrgId);
      refreshed = res.status !== 401;
    } catch {
      refreshed = false;
    }
    if (!refreshed) onUnauthorized?.();
  }
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(detail || `Request failed: ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  // Chat
  chat: (message: string, conversationId?: string) =>
    request<ChatResponse>("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, conversation_id: conversationId }),
    }),
  confirm: (conversationId: string, approved: boolean) =>
    request<ChatResponse>("/chat/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversation_id: conversationId, approved }),
    }),

  // Streaming chat over WebSocket.
  // Returns the open WebSocket. The caller must authenticate by sending
  // {action:"auth", token} immediately after connecting — handled here on
  // open with a FRESH token from the registered getter (plus the active-org
  // hint when one is selected).
  openChatStream: (handlers: ChatStreamHandlers): WebSocket => {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${proto}//${window.location.host}/ws/chat`;
    const ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      void (async () => {
        try {
          const tokenValue = await tokenGetter();
          const authMessage: Record<string, unknown> = { action: "auth", token: tokenValue };
          if (activeOrgId) authMessage.org_id = activeOrgId;
          ws.send(JSON.stringify(authMessage));
        } catch {
          handlers.onError?.("Sign-in required");
          ws.close();
        }
      })();
    };
    ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        switch (data.type) {
          case "sources":
            handlers.onSources?.(data.sources || [], data.conversation_id);
            break;
          case "token":
            handlers.onToken?.(data.content);
            break;
          case "tool_proposal":
            handlers.onToolProposal?.(data as ToolProposal);
            break;
          case "tool_result":
            handlers.onToolResult?.(data as ToolResultEvent);
            break;
          case "message":
            handlers.onMessage?.(data);
            break;
          case "error":
            handlers.onError?.(data.detail || "Unknown error");
            break;
        }
      } catch {
        /* ignore malformed frames */
      }
    };
    ws.onerror = () => handlers.onError?.("WebSocket error");
    return ws;
  },

  // Send a chat message over an existing authenticated WS.
  sendChat: (ws: WebSocket, message: string, conversationId: string | null) =>
    ws.send(JSON.stringify({ action: "chat", message, conversation_id: conversationId })),

  // Confirm or decline a proposed tool over an existing authenticated WS.
  confirmTool: (ws: WebSocket, conversationId: string, toolId: string, approved: boolean) =>
    ws.send(JSON.stringify({ action: "tool_confirm", conversation_id: conversationId, tool_id: toolId, approved })),

  // Ingestion
  ingestDrive: (folderId?: string) =>
    request<IngestResponse>(`/ingest${folderId ? `?folder_id=${folderId}` : ""}`, { method: "POST" }),
  ingestSlack: () => request<IngestResponse>("/ingest/slack", { method: "POST" }),
  ingestAsync: (source: "drive" | "slack", folderId?: string) =>
    request<IngestResponse>(`/ingest/async?source=${source}${folderId ? `&folder_id=${folderId}` : ""}`, {
      method: "POST",
    }),

  // Synthesis
  synthesize: () => request<SynthesisResponse>("/synthesize", { method: "POST" }),
  synthesizeAsync: () => request<IngestResponse>("/synthesize/async", { method: "POST" }),

  // Jobs
  getJob: (jobId: string) => request<Record<string, unknown>>(`/jobs/${jobId}`),

  // Skills
  listSkills: () => request<{ skills: SkillSummary[] }>("/skills"),
  getSkill: (id: string) => request<SkillDetail>(`/skills/${id}`),
  updateSkill: (id: string, content: string) =>
    request<{ status: string }>(`/skills/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    }),
  approveSkill: (id: string) => request<{ status: string }>(`/skills/${id}/approve`, { method: "POST" }),
  rejectSkill: (id: string) => request<{ status: string }>(`/skills/${id}/reject`, { method: "POST" }),

  // Conversations
  listConversations: () => request<{ conversations: ConversationSummary[] }>("/conversations"),

  // Auth / org context
  getMyOrg: () => request<MyOrg>("/orgs/me"),
  listMyOrgs: () => request<{ orgs: MyOrgSummary[] }>("/orgs"),

  // Dev mode only (no VITE_AUTH0_* config): keep the paste-token flow.
  setToken: (t: string) => localStorage.setItem("auth_token", t),
  getToken: () => localStorage.getItem("auth_token") || "",
  clearToken: () => localStorage.removeItem("auth_token"),
};