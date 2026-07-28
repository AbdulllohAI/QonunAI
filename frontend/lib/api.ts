import type {
  DocumentAnalysisResult,
  Language,
  LegalAct,
  LegalAnswer,
  Mode,
  SearchResponse,
} from "./types";

export const API_URL =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

const V1 = `${API_URL}/api/v1`;

function authHeaders(): Record<string, string> {
  if (typeof window === "undefined") return {};
  const token = window.localStorage.getItem("uzlex_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${V1}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status}: ${detail.slice(0, 300)}`);
  }
  return res.json() as Promise<T>;
}

/** Events emitted by the SSE chat stream. */
export interface StreamHandlers {
  onMeta?: (data: { conversation_id: string; language: string }) => void;
  onSources?: (data: { sources: LegalAnswer["citations"]; retrieval_ms: number }) => void;
  onDelta?: (text: string) => void;
  onDone?: (result: LegalAnswer) => void;
  onError?: (error: string) => void;
}

/**
 * Stream a chat answer.
 *
 * Note: the `done` event carries the *validated* answer. Replace whatever was
 * streamed with `result.answer` — server-side validation strips fabricated
 * citations, so the streamed text can differ from the final one.
 */
export async function streamChat(
  body: {
    message: string;
    conversation_id?: string | null;
    mode?: Mode;
    language?: Language | null;
    provider?: string | null;
  },
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${V1}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ ...body, stream: true }),
    signal,
  });

  if (!res.ok || !res.body) {
    const detail = await res.text().catch(() => res.statusText);
    handlers.onError?.(`${res.status}: ${detail.slice(0, 300)}`);
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by a blank line.
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";

    for (const frame of frames) {
      let event = "message";
      const dataLines: string[] = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (!dataLines.length) continue;

      let payload: any;
      try {
        payload = JSON.parse(dataLines.join("\n"));
      } catch {
        continue;
      }

      switch (event) {
        case "meta":
          handlers.onMeta?.(payload);
          break;
        case "sources":
          handlers.onSources?.(payload);
          break;
        case "delta":
          handlers.onDelta?.(payload.text ?? "");
          break;
        case "done":
          handlers.onDone?.(payload.result as LegalAnswer);
          break;
        case "error":
          handlers.onError?.(payload.error ?? "unknown error");
          break;
      }
    }
  }
}

export const api = {
  chat: (body: Record<string, unknown>) =>
    request<LegalAnswer & { conversation_id: string; message_id: string }>("/chat", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  search: (body: {
    query: string;
    language?: Language | null;
    top_k?: number;
    act_types?: string[] | null;
    in_force_only?: boolean;
  }) => request<SearchResponse>("/search", { method: "POST", body: JSON.stringify(body) }),

  byArticle: (body: {
    article_number: string;
    act_name?: string | null;
    act_id?: string | null;
    language?: Language | null;
    explain?: boolean;
  }) => request<any>("/search/article", { method: "POST", body: JSON.stringify(body) }),

  listActs: (params: { act_type?: string; q?: string; limit?: number } = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v != null) as [string, string][],
    );
    return request<LegalAct[]>(`/laws?${qs}`);
  },

  actTree: (actId: string, language: Language = "uz-Latn", includeBody = false) =>
    request<any[]>(
      `/laws/${actId}/tree?language=${language}&include_body=${includeBody}`,
    ),

  articleTimeline: (actId: string, article: string, language: Language = "uz-Latn") =>
    request<any>(`/laws/${actId}/articles/${article}/timeline?language=${language}`),

  corpusStats: () => request<any>("/laws/stats"),

  alerts: (days = 30) => request<any[]>(`/alerts?days=${days}`),

  analyzeDocument: async (
    file: File,
    opts: { question?: string; language?: Language | null } = {},
  ): Promise<DocumentAnalysisResult> => {
    const form = new FormData();
    form.append("file", file);
    if (opts.question) form.append("question", opts.question);
    if (opts.language) form.append("language", opts.language);

    const res = await fetch(`${V1}/documents/analyze`, {
      method: "POST",
      headers: authHeaders(),
      body: form,
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => res.statusText);
      throw new Error(`${res.status}: ${detail.slice(0, 400)}`);
    }
    return res.json();
  },

  health: () => fetch(`${API_URL}/health`).then((r) => r.json()),
};
