# Code review: Audit Tools

**Дата:** 27 августа 2026  
**Объект:** `audit-tools`, `main`, демо v0.0.1  
**Объём:** ~12–14 тыс. строк своего Python (backend + Pipe + тесты), без форка Open WebUI  
**Вердикт:** сильный доменный прототип. Для демо СВА — готов. Для контура банка — нет: API без auth.

Шкала 1–10 — код и инженерная гигиена, не качество юридической выдачи модели.

| Измерение | Балл | Комментарий |
|---|---|---|
| Продуктовая архитектура | **8** | Сборка, не форк. HITL, норма ≠ факт, allowlist — в коде |
| Доменный backend | **8** | Knowledge разрезан. Артефакты — общий runner |
| Pipe «Аудитор» | **7** | Маршрутизация в `intent.py` с тестами; HTTP-клей ~1.1k |
| Retrieval / RAG | **7** | Hybrid + RRF + rerank + MMR. Ask отказывает без evidence. Eval 20 вопросов |
| Документы Word/Excel | **7** | Тесты на парсер и docx. Генераторы тяжёлые, изолированы |
| Тесты | **7** | URL/docx + Pipe + retrieve/ask-refuse + eval_rag + TestClient + concurrent save |
| Безопасность контура | **6** | Loopback, CORS не `*`, DDG/Bing за флагом, hop в allowlist, `case_id` без `../`. Нет auth |
| Операционка / DX | **10** | CI: ruff + mypy + pytest; request id; API 0.0.1 |
| Документация | **8** | Видение, гайд, RAG, промпты согласованы с кодом |
| **Итого как v0.0.1** | **8** | Демо выше среднего. Не «коробка в банк» |
| **Итого как продукт банка** | **5** | Нет auth на API |

---

## Что сделано правильно

1. **Граница кода.** FastAPI — система записи. Open WebUI — витрина. Pipe вызывает API, не дублирует download/RAG.
2. **HITL в коде.** Скачивание только после `/select`. Мнение только после `утверждаю гипотезы`.
3. **Allowlist на hop редиректа** + curated `known_sources`. Поиск по умолчанию SearXNG; DDG/Bing за `NPA_WEB_FALLBACK`.
4. **RAG не top-k cosine:** multi-query, BM25 + dense, RRF, rerank, MMR. Пустая evidence → отказ, не преамбула кодекса.
5. **Промпты снаружи** (`docs/prompts`, ~31 файл). Артефакты через `run_llm_artifact_events`.
6. **Тесты на боли продукта**, не `assert True`. CI живой.

---

## Что ещё открыто

### P0 — без этого в банк нельзя

**CR-02. API без аутентификации.** CORS и loopback-порты уже сузили. Open WebUI свой auth **не** защищает backend: с машины аудитора любой процесс бьёт в `:8100`. Нужен shared secret из env.

### P1 — будет стоить недель

| ID | Суть |
|---|---|
| CR-09 | Нет полного TestClient create → select с mock propose. Live eval не гейт CI |
| CR-10 | Нет экспорта метрик времени; request id не в Ollama payload |
| CR-12 | Pipe сеется только с `OPENWEBUI_API_KEY`. Расхождение `pipe_help.txt` и `HELP` в Pipe |
| — | Эмбеддинги всё ещё в `knowledge_index.json` |

Pipe по-прежнему NLP на regex — это не P1. Новый тип проверки не требует трогать парсер фраз; синонимы команд — да. Не ждать LangGraph.

### P2 — гигиена

| ID | Суть |
|---|---|
| CR-15 | `GET /` светит `data_root`, SearXNG, имена моделей |
| CR-18 | SearXNG `secret_key` из репо, `limiter: false` |
| CR-19 | Spoof `X-Forwarded-For: 127.0.0.1` к SearXNG |
| CR-21 | Дубль `find_best_url` / `build_search_queries` |
| CR-23 | `frontend/` в README, в git файлов нет |
| CR-24 | Широкий `except Exception` → 502 глотает баги |
| CR-26 | `conclusion_docx` / `brief_docx` — OOXML + домен в одном файле |

---

## Архитектура vs `ARCHITECTURE.md`

Совпадает: нет SPA, кейс на диске, Pipe = фазы, `вопрос` → `/ask`, промпты снаружи.

Расхождение, которое ещё важно: без ключа Pipe при `up` не появится; trail полный (file/chunk/url) есть только у download-manifest и `trail/ask.jsonl`.

---

## Дальше, по порядку

Не делать микросервисы, React, Postgres «на вырост».

1. Auth на API (секрет из env).
2. Засев Pipe без ручного ключа при первом `up`.
3. Пинить digest образов (`searxng:latest`, `open-webui:main`).
4. TestClient create → select без Ollama.
5. SQLite — когда появится второй пользователь или «открой кейс».
6. Клиентские данные / DuckDB — как в PLAN v0.4, не раньше стабильной цитаты.

**Не делать:** форк Open WebUI, второй агент (LangGraph), Excel клиента в Knowledge, новая vector DB «вместо eval», развитие `frontend/`.

---

## Тепловая карта

| Модуль | Здоровье |
|---|---|
| `domains` / `known_sources` / промпты / CI | отлично |
| retrieve, ask, ingest/index, `document_artifact`, storage, `intent.py` | хорошо |
| `npa_search`, Pipe HTTP-клей, `brief_docx` + `conclusion_docx` | средне |

Слабые места, которые ещё живы: JSON с векторами; два огромных docx; Pipe ~1.1k HTTP-клея.

---

## Итог

Проект не свалка скриптов. Есть продукт, инварианты, HITL, локальная модель, RAG с отказом и документация, с которой можно садиться к аудитору.

Планка СВА: закрыта. Планка банка: auth + пины образов.
