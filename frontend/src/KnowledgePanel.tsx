import { useEffect, useRef, useState } from "react";
import {
  knowledgeApi,
  type AskHit,
  type KnowledgeItem,
  type StreamEvent,
} from "./api";

type Props = {
  caseId: string;
  busy: boolean;
  setBusy: (v: boolean) => void;
  setError: (v: string | null) => void;
  onLog: (kind: "info" | "ok" | "error", text: string) => void;
  onStream: (ev: StreamEvent) => void;
};

type ChatTurn = { q: string; a: string; sources: AskHit[] };

export default function KnowledgePanel({
  caseId,
  busy,
  setBusy,
  setError,
  onLog,
  onStream,
}: Props) {
  const [items, setItems] = useState<KnowledgeItem[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [question, setQuestion] = useState(
    "Какие нормы ГК и НК важны для аренды нежилого помещения и валютных расчётов?"
  );
  const [chat, setChat] = useState<ChatTurn[]>([]);
  const [owKey, setOwKey] = useState("");
  const [owStatus, setOwStatus] = useState<string>("");
  const fileRef = useRef<HTMLInputElement>(null);

  async function reload() {
    const data = await knowledgeApi.get(caseId);
    setItems(data.items || []);
  }

  useEffect(() => {
    reload().catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [caseId]);

  async function onUpload(list: FileList | null) {
    if (!list?.length) return;
    setError(null);
    setBusy(true);
    try {
      const result = await knowledgeApi.upload(caseId, list);
      setItems(result.items);
      onLog("ok", `Загружено файлов: ${result.added.length}`);
      for (const err of result.errors || []) {
        onLog("error", `${err.filename}: ${err.error}`);
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      onLog("error", msg);
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function onBuild() {
    setError(null);
    setBusy(true);
    onLog("info", "Сборка базы знаний: текст → саммари → индекс");
    try {
      const result = await knowledgeApi.buildStream(caseId, (ev) => {
        onStream(ev);
        if (ev.type === "summary") {
          onLog(ev.status === "ok" ? "ok" : "error", `Саммари: ${ev.title || ""}`);
        }
      });
      if (result.items.length) setItems(result.items);
      else await reload();
      onLog("ok", "База знаний собрана");
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      onLog("error", msg);
    } finally {
      setBusy(false);
    }
  }

  async function onAsk() {
    const q = question.trim();
    if (!q) return;
    setError(null);
    setBusy(true);
    onLog("info", `Вопрос к базе: ${q}`);
    try {
      const result = await knowledgeApi.ask(caseId, q);
      setChat((prev) => [...prev, { q, a: result.answer, sources: result.sources || [] }]);
      onLog("ok", "Ответ получен");
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      onLog("error", msg);
    } finally {
      setBusy(false);
    }
  }

  async function onExport() {
    try {
      await knowledgeApi.exportPack(caseId);
      onLog("ok", "Пакет для Open WebUI скачан");
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
    }
  }

  async function onSync() {
    setError(null);
    setBusy(true);
    try {
      const st = await knowledgeApi.openwebuiStatus();
      setOwStatus(
        st.reachable
          ? st.auth
            ? "Open WebUI: доступен, ключ принят"
            : "Open WebUI доступен — нужен API ключ (Settings → Account)"
          : `Open WebUI недоступен (${st.url})`
      );
      const result = await knowledgeApi.openwebuiSync(caseId, owKey.trim() || undefined);
      onLog("ok", `Коллекция: ${result.name}, файлов: ${result.uploaded.length}`);
      setOwStatus(`Готово: ${result.name}. В чате нажмите # и выберите коллекцию.`);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      onLog("error", msg);
    } finally {
      setBusy(false);
    }
  }

  const summarized = items.filter((i) => i.summary_status === "ok").length;

  return (
    <div className="card">
      <h2>4. База знаний и саммари</h2>
      <p className="muted">
        Саммари — по теме проверки, не весь кодекс. Можно докинуть файлы, которые не скачались.
      </p>

      <div className="actions" style={{ marginBottom: 12 }}>
        <input
          ref={fileRef}
          type="file"
          multiple
          hidden
          accept=".pdf,.docx,.txt,.html,.htm,.md,.rtf"
          disabled={busy}
          onChange={(e) => onUpload(e.target.files)}
        />
        <button
          type="button"
          className="secondary"
          disabled={busy}
          onClick={() => fileRef.current?.click()}
        >
          Загрузить НПА
        </button>
        <button type="button" onClick={onBuild} disabled={busy || items.length === 0}>
          {busy ? "Собираю базу…" : "Собрать базу и саммари"}
        </button>
      </div>

      <div className="meta">
        <span className="chip">документов: {items.length}</span>
        <span className="chip">саммари: {summarized}</span>
      </div>

      {items.map((item) => (
        <div key={item.id} className="kb-item">
          <button
            type="button"
            className="kb-head"
            onClick={() => setOpenId((id) => (id === item.id ? null : item.id))}
          >
            <span>
              <span className={`priority ${item.source === "uploaded" ? "p2" : "p3"}`}>
                {item.source === "uploaded" ? "загружен" : "скачан"}
              </span>
              {item.title}
            </span>
            <span className="muted">
              {item.summary_status || item.extract_status || ""}
            </span>
          </button>
          {openId === item.id && (
            <div className="kb-body">
              {item.extract_error && (
                <div className="status-failed">{item.extract_error}</div>
              )}
              {item.summary_error && (
                <div className="status-failed">{item.summary_error}</div>
              )}
              {item.summary ? (
                <pre className="summary">{item.summary}</pre>
              ) : (
                <p className="muted">Саммари ещё нет — нажмите «Собрать базу и саммари».</p>
              )}
            </div>
          )}
        </div>
      ))}

      <h3 className="kb-h3">Вопросы по НПА</h3>
      <textarea
        value={question}
        onChange={(e) => setQuestion(e.target.value)}
        disabled={busy}
        rows={3}
      />
      <div className="actions">
        <button type="button" onClick={onAsk} disabled={busy || items.length === 0}>
          Спросить базу
        </button>
      </div>
      {chat.map((t, i) => (
        <div key={i} className="qa">
          <div className="qa-q">{t.q}</div>
          <pre className="summary">{t.a}</pre>
          {!!t.sources.length && (
            <div className="muted">
              Источники: {t.sources.map((s) => `[${s.n}] ${s.title}`).join(" · ")}
            </div>
          )}
        </div>
      ))}

      <h3 className="kb-h3">Open WebUI (RAG в чате)</h3>
      <p className="muted">
        Скачайте пакет и загрузите в Workspace → Knowledge, либо синхронизируйте по API-ключу
        (Settings → Account → API Keys). В чате выберите коллекцию через #.
      </p>
      {owStatus && <div className="okbox">{owStatus}</div>}
      <input
        placeholder="API ключ Open WebUI (необязательно)"
        value={owKey}
        onChange={(e) => setOwKey(e.target.value)}
        disabled={busy}
      />
      <div className="actions">
        <button type="button" className="secondary" onClick={onExport} disabled={busy}>
          Скачать пакет для Open WebUI
        </button>
        <button type="button" onClick={onSync} disabled={busy}>
          Отправить в Open WebUI
        </button>
        <a className="btn-link" href="http://localhost:3000" target="_blank" rel="noreferrer">
          Открыть Open WebUI
        </a>
      </div>
    </div>
  );
}
