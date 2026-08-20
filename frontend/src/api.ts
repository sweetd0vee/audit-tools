export type DocStatus =
  | "ok"
  | "failed"
  | "not_found"
  | "searching"
  | "downloading"
  | "skipped"
  | null
  | undefined;

export type ProposedDocument = {
  id: string;
  title: string;
  doc_type: string;
  why_needed: string;
  search_queries: string[];
  priority: number;
  selected: boolean;
  found_url?: string | null;
  local_path?: string | null;
  download_status?: DocStatus;
  download_error?: string | null;
};

export type KnowledgeItem = {
  id: string;
  title: string;
  source: string;
  filename: string;
  extract_status?: string | null;
  extract_error?: string | null;
  summary?: string | null;
  summary_status?: string | null;
  summary_error?: string | null;
  char_count?: number;
  chunk_count?: number;
};

export type CaseState = {
  case_id: string;
  status: string;
  inspection_name: string;
  keywords: string[];
  period?: string | null;
  topics: string[];
  documents: ProposedDocument[];
};

export type StreamEvent = {
  type: string;
  message?: string;
  role?: string;
  content?: string;
  elapsed_ms?: number;
  case_id?: string;
  status?: string;
  title?: string;
  documents?: ProposedDocument[];
  knowledge?: KnowledgeItem[];
  raw_topics?: string[];
  model?: string;
  payload?: Record<string, unknown>;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data?.detail || res.statusText || "Request failed";
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data as T;
}

export const api = {
  createCase: (body: {
    inspection_name: string;
    keywords: string[];
    period?: string;
  }) =>
    request<{ case_id: string; status: string }>("/api/v1/cases", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  proposeStream: async (
    caseId: string,
    onEvent: (ev: StreamEvent) => void
  ): Promise<{
    documents: ProposedDocument[];
    raw_topics: string[];
    model: string;
    elapsed_ms?: number;
  }> => {
    const res = await fetch(`/api/v1/cases/${caseId}/propose/stream`);
    if (!res.ok || !res.body) {
      const text = await res.text();
      throw new Error(text || `Stream failed: ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let saved: {
      documents: ProposedDocument[];
      raw_topics: string[];
      model: string;
      elapsed_ms?: number;
    } | null = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() || "";

      for (const chunk of chunks) {
        const line = chunk
          .split("\n")
          .map((l) => l.trim())
          .find((l) => l.startsWith("data:"));
        if (!line) continue;
        const raw = line.slice(5).trim();
        if (!raw) continue;
        const ev = JSON.parse(raw) as StreamEvent;
        onEvent(ev);
        if (ev.type === "saved") {
          saved = {
            documents: ev.documents || [],
            raw_topics: ev.raw_topics || [],
            model: ev.model || "",
            elapsed_ms: ev.elapsed_ms,
          };
        }
        if (ev.type === "error") {
          throw new Error(ev.message || "Propose stream error");
        }
      }
    }

    if (!saved) throw new Error("Stream finished without saved result");
    return saved;
  },

  select: (caseId: string, document_ids: string[], manual_urls: Record<string, string> = {}) =>
    request<{ case_id: string; selected_count: number; documents: ProposedDocument[] }>(
      `/api/v1/cases/${caseId}/select`,
      {
        method: "POST",
        body: JSON.stringify({ document_ids, manual_urls }),
      }
    ),

  download: (caseId: string) =>
    request<{
      case_id: string;
      status: string;
      downloaded: number;
      failed: number;
      library_dir: string;
      archive_name?: string | null;
      archive_url?: string | null;
      documents: ProposedDocument[];
    }>(`/api/v1/cases/${caseId}/download`, { method: "POST" }),

  library: (caseId: string) =>
    request<{
      library_dir: string;
      files: string[];
      archive_name?: string | null;
      archive_url?: string | null;
      documents: ProposedDocument[];
    }>(`/api/v1/cases/${caseId}/library`),

  downloadArchive: async (caseId: string, fallbackName?: string) => {
    const res = await fetch(`/api/v1/cases/${caseId}/library/archive`);
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || `Archive download failed: ${res.status}`);
    }
    const blob = await res.blob();
    const header = res.headers.get("content-disposition") || "";
    const utfMatch = header.match(/filename\*=UTF-8''([^;]+)/i);
    const plainMatch = header.match(/filename="?([^";]+)"?/i);
    let name = fallbackName || `library_${caseId}.zip`;
    if (utfMatch?.[1]) {
      try {
        name = decodeURIComponent(utfMatch[1]);
      } catch {
        name = utfMatch[1];
      }
    } else if (plainMatch?.[1]) {
      name = plainMatch[1];
    }
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    return name;
  },
};

export type AskHit = {
  n: number;
  title: string;
  filename?: string;
  excerpt: string;
};

async function consumeSse(
  res: Response,
  onEvent: (ev: StreamEvent) => void
): Promise<StreamEvent | null> {
  if (!res.ok || !res.body) {
    const text = await res.text();
    throw new Error(text || `Stream failed: ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let lastSaved: StreamEvent | null = null;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() || "";
    for (const chunk of chunks) {
      const line = chunk
        .split("\n")
        .map((l) => l.trim())
        .find((l) => l.startsWith("data:"));
      if (!line) continue;
      const raw = line.slice(5).trim();
      if (!raw) continue;
      const ev = JSON.parse(raw) as StreamEvent;
      onEvent(ev);
      if (ev.type === "saved") lastSaved = ev;
      if (ev.type === "error") throw new Error(ev.message || "Stream error");
    }
  }
  return lastSaved;
}

export const knowledgeApi = {
  get: (caseId: string) =>
    request<{
      items: KnowledgeItem[];
      openwebui_knowledge_id?: string | null;
      openwebui_knowledge_name?: string | null;
    }>(`/api/v1/cases/${caseId}/knowledge`),

  upload: async (caseId: string, files: FileList | File[]) => {
    const body = new FormData();
    for (const f of Array.from(files)) body.append("files", f);
    const res = await fetch(`/api/v1/cases/${caseId}/knowledge/upload`, {
      method: "POST",
      body,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = data?.detail || res.statusText;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data as { items: KnowledgeItem[]; added: KnowledgeItem[]; errors: { filename?: string; error: string }[] };
  },

  buildStream: async (caseId: string, onEvent: (ev: StreamEvent) => void) => {
    const res = await fetch(`/api/v1/cases/${caseId}/knowledge/build/stream`);
    const saved = await consumeSse(res, onEvent);
    return { items: saved?.knowledge || [] };
  },

  ask: (caseId: string, question: string) =>
    request<{
      answer: string;
      sources: AskHit[];
      model: string;
      used_embeddings: boolean;
    }>(`/api/v1/cases/${caseId}/knowledge/ask`, {
      method: "POST",
      body: JSON.stringify({ question }),
    }),

  exportPack: async (caseId: string) => {
    const res = await fetch(`/api/v1/cases/${caseId}/knowledge/export`);
    if (!res.ok) throw new Error(await res.text());
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `kb_${caseId}.zip`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  },

  openwebuiStatus: () =>
    request<{ reachable: boolean; auth: boolean; url: string; error?: string }>(
      "/api/v1/knowledge/openwebui/status"
    ),

  openwebuiSync: (caseId: string, api_key?: string) =>
    request<{ knowledge_id: string; name: string; url: string; uploaded: { file: string }[] }>(
      `/api/v1/cases/${caseId}/knowledge/openwebui/sync`,
      { method: "POST", body: JSON.stringify({ api_key: api_key || null }) }
    ),
};
