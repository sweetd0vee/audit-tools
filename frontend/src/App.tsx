import { useEffect, useMemo, useRef, useState } from "react";
import { api, type ProposedDocument, type StreamEvent } from "./api";
import KnowledgePanel from "./KnowledgePanel";
import DevLogPanel, { type LogEntry } from "./DevLogPanel";

type Step = "form" | "list" | "done";

function priorityLabel(p: number) {
  if (p === 1) return "обязательно";
  if (p === 2) return "желательно";
  return "опционально";
}

function nowTime() {
  return new Date().toLocaleTimeString("ru-RU", { hour12: false });
}

export default function App() {
  const [inspectionName, setInspectionName] = useState(
    "Проверка аренды коммерческой недвижимости"
  );
  const [keywordsText, setKeywordsText] = useState(
    "аренда, валютные расчёты, НДС, нежилое помещение"
  );
  const [period, setPeriod] = useState("2025");

  const [caseId, setCaseId] = useState<string | null>(null);
  const [topics, setTopics] = useState<string[]>([]);
  const [model, setModel] = useState("");
  const [docs, setDocs] = useState<ProposedDocument[]>([]);
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [manualUrls, setManualUrls] = useState<Record<string, string>>({});
  const [archiveName, setArchiveName] = useState("");
  const [downloadStats, setDownloadStats] = useState<{ ok: number; failed: number } | null>(
    null
  );

  const [step, setStep] = useState<Step>("form");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [logEntries, setLogEntries] = useState<LogEntry[]>([]);
  const [streamingText, setStreamingText] = useState("");
  const [liveElapsedMs, setLiveElapsedMs] = useState(0);
  const [logOpen, setLogOpen] = useState(true);
  const timerStart = useRef<number | null>(null);
  const logId = useRef(0);

  const selectedCount = useMemo(
    () => Object.values(selected).filter(Boolean).length,
    [selected]
  );

  useEffect(() => {
    if (!busy) return;
    timerStart.current = performance.now();
    setLiveElapsedMs(0);
    const id = window.setInterval(() => {
      if (timerStart.current != null) {
        setLiveElapsedMs(Math.floor(performance.now() - timerStart.current));
      }
    }, 100);
    return () => window.clearInterval(id);
  }, [busy]);

  function pushLog(
    kind: LogEntry["kind"],
    text: string,
    opts?: { role?: string; elapsedMs?: number }
  ) {
    logId.current += 1;
    const elapsed =
      opts?.elapsedMs ??
      (timerStart.current != null
        ? Math.floor(performance.now() - timerStart.current)
        : liveElapsedMs);
    setLogEntries((prev) => [
      ...prev,
      {
        id: logId.current,
        at: nowTime(),
        elapsedMs: elapsed,
        kind,
        role: opts?.role,
        text,
      },
    ]);
  }

  function parseKeywords(text: string) {
    return text
      .split(/[,;\n]/)
      .map((x) => x.trim())
      .filter(Boolean);
  }

  function handleStreamEvent(ev: StreamEvent) {
    const elapsed = ev.elapsed_ms ?? liveElapsedMs;
    if (ev.type === "status" && ev.message) {
      pushLog("info", ev.message, { elapsedMs: elapsed });
    }
    if (ev.type === "chat" && ev.content) {
      pushLog("chat", ev.content, { role: ev.role, elapsedMs: elapsed });
      if (ev.role === "assistant") setStreamingText("");
    }
    if (ev.type === "token" && ev.content) {
      setStreamingText((prev) => prev + ev.content);
    }
    if (ev.type === "result") {
      pushLog(
        "ok",
        `Модель завершила ответ за ${elapsed} ms`,
        { elapsedMs: elapsed }
      );
    }
    if (ev.type === "saved") {
      pushLog(
        "ok",
        `Сохранено в кейс: ${(ev.documents || []).length} документов`,
        { elapsedMs: elapsed }
      );
    }
  }

  async function onPropose() {
    setError(null);
    timerStart.current = performance.now();
    setLiveElapsedMs(0);
    setBusy(true);
    setDownloadStats(null);
    setArchiveName("");
    setStreamingText("");
    setLogEntries([]);
    pushLog("info", "Старт: создание кейса и propose через LLM");

    try {
      const created = await api.createCase({
        inspection_name: inspectionName.trim(),
        keywords: parseKeywords(keywordsText),
        period: period.trim() || undefined,
      });
      setCaseId(created.case_id);
      pushLog("ok", `Кейс создан: ${created.case_id}`);

      const proposed = await api.proposeStream(created.case_id, handleStreamEvent);
      setDocs(proposed.documents);
      setTopics(proposed.raw_topics || []);
      setModel(proposed.model || "");
      if (proposed.elapsed_ms != null) setLiveElapsedMs(proposed.elapsed_ms);

      const initial: Record<string, boolean> = {};
      for (const d of proposed.documents) {
        initial[d.id] = d.priority === 1;
      }
      setSelected(initial);
      setManualUrls({});
      setStep("list");
      pushLog("ok", "Готово: список документов для валидации");
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      pushLog("error", msg);
    } finally {
      setBusy(false);
      setStreamingText("");
    }
  }

  function toggle(id: string) {
    setSelected((prev) => ({ ...prev, [id]: !prev[id] }));
  }

  function selectAll(value: boolean) {
    const next: Record<string, boolean> = {};
    for (const d of docs) next[d.id] = value;
    setSelected(next);
  }

  async function onDownload() {
    if (!caseId) return;
    setError(null);
    timerStart.current = performance.now();
    setLiveElapsedMs(0);
    setBusy(true);
    setStreamingText("");
    pushLog("info", "Старт: select + download выбранных документов");

    try {
      const ids = docs.filter((d) => selected[d.id]).map((d) => d.id);
      if (!ids.length) throw new Error("Выберите хотя бы один документ");

      const urls: Record<string, string> = {};
      for (const id of ids) {
        const u = (manualUrls[id] || "").trim();
        if (u) urls[id] = u;
      }

      pushLog("info", `Select: ${ids.length} документов`);
      await api.select(caseId, ids, urls);
      pushLog("ok", "Select сохранён, начинаю download…");

      const result = await api.download(caseId);
      setDocs(result.documents);
      setDownloadStats({ ok: result.downloaded, failed: result.failed });
      setArchiveName(result.archive_name || "");

      for (const d of result.documents.filter((x) => x.selected)) {
        pushLog(
          d.download_status === "ok" ? "ok" : "error",
          `${d.download_status}: ${d.title}` +
            (d.found_url ? `\n${d.found_url}` : "") +
            (d.download_error ? `\n${d.download_error}` : "")
        );
      }

      const lib = await api.library(caseId);
      if (lib.archive_name) setArchiveName(lib.archive_name);
      setStep("done");
      pushLog(
        "ok",
        `Библиотека: скачано ${result.downloaded}, ошибок ${result.failed}`
      );

      if (result.downloaded > 0) {
        const saved = await api.downloadArchive(caseId, result.archive_name || undefined);
        setArchiveName(saved);
        pushLog("ok", `Архив сохранён: ${saved}`);
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      pushLog("error", msg);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="layout">
      <main className="main">
        <h1>Audit Tools — библиотека НПА</h1>
        <p className="sub">
          Шаг 1–2: документы → архив → саммари и RAG-база НПА
        </p>

        {error && <div className="error">{error}</div>}

        <div className="card">
          <h2>1. Параметры проверки</h2>
          <label htmlFor="name">Название проверки</label>
          <input
            id="name"
            value={inspectionName}
            onChange={(e) => setInspectionName(e.target.value)}
            disabled={busy}
          />

          <label htmlFor="keywords">Ключевые слова (через запятую)</label>
          <textarea
            id="keywords"
            value={keywordsText}
            onChange={(e) => setKeywordsText(e.target.value)}
            disabled={busy}
          />

          <div className="row">
            <div>
              <label htmlFor="period">Период</label>
              <input
                id="period"
                value={period}
                onChange={(e) => setPeriod(e.target.value)}
                disabled={busy}
              />
            </div>
          </div>

          <div className="actions">
            <button onClick={onPropose} disabled={busy || !inspectionName.trim()}>
              {busy && step === "form"
                ? "Модель думает… смотрите лог →"
                : "Предложить документы"}
            </button>
          </div>
        </div>

        {(step === "list" || step === "done") && (
          <div className="card">
            <div className="toolbar">
              <h2 style={{ margin: 0 }}>2. Валидация списка</h2>
              <div className="actions">
                <button
                  className="secondary"
                  type="button"
                  onClick={() => selectAll(true)}
                  disabled={busy}
                >
                  Выбрать все
                </button>
                <button
                  className="secondary"
                  type="button"
                  onClick={() => selectAll(false)}
                  disabled={busy}
                >
                  Снять все
                </button>
              </div>
            </div>

            <div className="meta">
              {caseId && <span className="chip">case: {caseId}</span>}
              {model && <span className="chip">model: {model}</span>}
              <span className="chip">выбрано: {selectedCount}</span>
            </div>

            {!!topics.length && <p className="muted">Темы: {topics.join(" · ")}</p>}

            {docs.map((d) => (
              <label key={d.id} className={`doc ${selected[d.id] ? "selected" : ""}`}>
                <input
                  type="checkbox"
                  checked={!!selected[d.id]}
                  onChange={() => toggle(d.id)}
                  disabled={busy || step === "done"}
                />
                <div>
                  <div className="doc-title">
                    <span className={`priority p${d.priority}`}>{priorityLabel(d.priority)}</span>
                    {d.title}
                  </div>
                  <div className="doc-why">{d.why_needed}</div>
                  <div className="muted">
                    {d.doc_type}
                    {d.download_status ? (
                      <>
                        {" · "}
                        <span className={`status-${d.download_status}`}>
                          {d.download_status}
                        </span>
                      </>
                    ) : null}
                  </div>
                  {selected[d.id] && step !== "done" && (
                    <input
                      className="url-input"
                      placeholder="Опционально: ручной URL"
                      value={manualUrls[d.id] || ""}
                      onChange={(e) =>
                        setManualUrls((prev) => ({ ...prev, [d.id]: e.target.value }))
                      }
                      disabled={busy}
                    />
                  )}
                  {d.found_url && (
                    <div className="muted" style={{ marginTop: 6 }}>
                      URL: {d.found_url}
                    </div>
                  )}
                  {d.download_error && (
                    <div className="status-failed" style={{ marginTop: 6, fontSize: "0.85rem" }}>
                      {d.download_error}
                    </div>
                  )}
                </div>
              </label>
            ))}

            {step === "list" && (
              <div className="actions">
                <button onClick={onDownload} disabled={busy || selectedCount === 0}>
                  {busy ? "Скачиваю…" : "Скачать выбранные"}
                </button>
              </div>
            )}
          </div>
        )}

        {step === "done" && (
          <div className="card">
            <h2>3. Библиотека кейса</h2>
            {downloadStats && (
              <div className="okbox">
                Скачано: {downloadStats.ok}, ошибок: {downloadStats.failed}
              </div>
            )}
            {archiveName ? (
              <p>
                Архив: <strong>{archiveName}</strong>
              </p>
            ) : (
              <p className="muted">Архив не собран — нет успешно скачанных файлов</p>
            )}
            <div className="actions" style={{ marginTop: 12 }}>
              {caseId && archiveName && (
                <button
                  type="button"
                  onClick={async () => {
                    try {
                      const saved = await api.downloadArchive(caseId, archiveName);
                      setArchiveName(saved);
                    } catch (e) {
                      setError(e instanceof Error ? e.message : String(e));
                    }
                  }}
                >
                  Скачать архив
                </button>
              )}
              <button
                className="secondary"
                type="button"
                onClick={() => {
                  setStep("form");
                  setCaseId(null);
                  setDocs([]);
                  setArchiveName("");
                  setDownloadStats(null);
                }}
              >
                Новая проверка
              </button>
            </div>
          </div>
        )}

        {step === "done" && caseId && (
          <KnowledgePanel
            caseId={caseId}
            busy={busy}
            setBusy={setBusy}
            setError={setError}
            onLog={(kind, text) => pushLog(kind, text)}
            onStream={handleStreamEvent}
          />
        )}
      </main>

      <DevLogPanel
        entries={logEntries}
        liveElapsedMs={liveElapsedMs}
        busy={busy}
        streamingText={streamingText}
        open={logOpen}
        onToggle={() => setLogOpen((v) => !v)}
      />
    </div>
  );
}
