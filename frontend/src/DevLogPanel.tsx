import { useEffect, useRef, useState } from "react";

export type LogEntry = {
  id: number;
  at: string;
  elapsedMs: number;
  kind: "info" | "chat" | "token" | "ok" | "error";
  role?: string;
  text: string;
};

type Props = {
  entries: LogEntry[];
  liveElapsedMs: number;
  busy: boolean;
  streamingText: string;
  open: boolean;
  onToggle: () => void;
};

function formatMs(ms: number) {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const rs = s % 60;
  const rms = ms % 1000;
  if (m > 0) return `${m}:${String(rs).padStart(2, "0")}.${String(rms).padStart(3, "0")}`;
  return `${rs}.${String(rms).padStart(3, "0")}s`;
}

export default function DevLogPanel({
  entries,
  liveElapsedMs,
  busy,
  streamingText,
  open,
  onToggle,
}: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [entries, streamingText, open]);

  // Auto-open when work starts
  useEffect(() => {
    if (busy && !open) onToggle();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busy]);

  return (
    <>
      {open && <div className="log-backdrop" onClick={onToggle} aria-hidden />}

      <div className={`log-drawer ${open ? "open" : "closed"}`}>
        <button
          type="button"
          className="log-tab"
          onClick={onToggle}
          aria-label={open ? "Скрыть лог" : "Показать лог"}
          title="лог"
        >
          <span className="log-tab-chevron">{open ? "›" : "‹"}</span>
          <span className="log-tab-icon" aria-hidden>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
              <path
                d="M7 3h7l5 5v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"
                stroke="#2f6fed"
                strokeWidth="1.8"
              />
              <path d="M14 3v5h5" stroke="#2f6fed" strokeWidth="1.8" />
            </svg>
          </span>
          <span className="log-tab-label">лог</span>
        </button>

        <aside className="log-panel">
          <div className="log-panel-inner">
            <div className="log-panel-head">
              <h2>ХОД РАБОТЫ</h2>
              <div className={`timer ${busy ? "running" : ""}`}>{formatMs(liveElapsedMs)}</div>
            </div>

            <p className="log-help">
              <strong>Как это работает.</strong> Слева — шаги проверки. Справа — живой лог: запросы
              к модели, стрим ответа, поиск и скачивание. Долгие паузы обычно значат запрос к{" "}
              <span className="log-link">Ollama</span>.
            </p>

            <div className="log-feed">
              {entries.length === 0 && !busy && (
                <div className="log-empty">Пока пусто — нажмите «Предложить документы».</div>
              )}

              {entries.map((e) => (
                <div key={e.id} className={`log-item kind-${e.kind}`}>
                  <div className="log-meta">
                    <span>{e.at}</span>
                    <span>+{formatMs(e.elapsedMs)}</span>
                    {e.role && <span className="role">{e.role}</span>}
                  </div>
                  {e.kind === "chat" ? (
                    <pre className={`bubble role-${e.role || "assistant"}`}>{e.text}</pre>
                  ) : (
                    <div className="log-text">{e.text}</div>
                  )}
                </div>
              ))}

              {busy && streamingText && (
                <div className="log-item kind-token">
                  <div className="log-meta">
                    <span>stream</span>
                    <span className="role">assistant</span>
                  </div>
                  <pre className="bubble role-assistant streaming">{streamingText}</pre>
                </div>
              )}
              <div ref={bottomRef} />
            </div>
          </div>
        </aside>
      </div>
    </>
  );
}

/** Optional local open state helper if parent doesn't manage it */
export function useLogOpen(defaultOpen = true) {
  const [open, setOpen] = useState(defaultOpen);
  return { open, setOpen, toggle: () => setOpen((v) => !v) };
}
