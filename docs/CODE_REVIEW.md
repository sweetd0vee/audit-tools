# Code review: Audit Tools

**Дата:** 27 августа 2026 (актуализация: вынесен `intent.py`, тесты фраз Pipe, засев `pipe-seed`)  
**Объект:** репозиторий `audit-tools`, ветка `main`, демо v0.0.1  
**Объём своего кода:** ~12–14 тыс. строк Python (backend + Pipe + тесты), без форка Open WebUI  
**Вердикт:** сильный доменный прототип с ясной архитектурой. Для демо СВА — готов. Для контура банка — нет: API без auth, нет eval цитаты как гейта релиза. Поиск по умолчанию локальный (SearXNG); DDG/Bing только по флагу.

Шкала: 1–10. Это оценка кода и инженерной гигиены, не качества юридической выдачи модели.

| Измерение | Балл | Комментарий |
|---|---|---|
| Продуктовая архитектура | **8** | Сборка, не форк. HITL, норма ≠ факт, allowlist — закодированы, не только в README |
| Доменный backend | **7** | Потоки документов и кейса читаются. Фасады `knowledge_*` тонкие, ядро раздуто |
| Pipe «Аудитор» | **7** | Тот же автомат, но маршрутизация в `intent.py` с тестами; HTTP-клей ~1.1k. Засев API, не ручной paste |
| Retrieval / RAG | **7** | Hybrid + RRF + rerank + MMR — выше среднего для v0. Дыры в отказе и eval |
| Документы Word/Excel | **7** | Много тестов на парсер и docx. Генераторы тяжёлые, но изолированы |
| Тесты | **6** | URL/docx + Pipe + retrieve/ask-refuse + 20 gold `eval_rag`. Нет TestClient API и concurrent save |
| Безопасность контура | **6** | Loopback bind, CORS не `*`, DDG/Bing за флагом, hop редиректа в allowlist. Нет auth на API |
| Операционка / DX | **10** | CI: ruff + mypy + pytest; логи с request id; версия API 0.0.1; PR режет `initial commit`. История main не схлопнута — это не дыра в tooling |
| Документация | **8** | Редкость: видение, гайд, RAG, промпты — согласованы с кодом |
| **Итого как v0.0.1** | **7** | Демо выше среднего. Не «коробка в банк» |
| **Итого как продукт банка** | **5** | Auth на API ещё нет. Локальность поиска — флаг `NPA_WEB_FALLBACK` (выкл. по умолчанию) |

---

## 1. Что здесь сделано хорошо

Не начинать ревью с «переписать». Каркас правильный.

1. **Граница своего кода.** FastAPI — система записи проверки. Open WebUI — витрина. Ollama / SearXNG — инфраструктура. Pipe вызывает API и не дублирует download/RAG. Это редкая дисциплина для LLM-демо.
2. **HITL в коде, не в промпте.** Скачивание только после `/select`. Мнение только после `утверждаю гипотезы`. Это инварианты продукта, и они соблюдены в `library_flow.run_select` и Pipe.
3. **Allowlist на двух уровнях** — `domains.host_allowed` + фильтр SearXNG. `usable_url` отсекает плейсхолдеры `https://pravo.by/...`, голые хосты, чужие домены. На это есть тесты.
4. **Curated `known_sources`.** Закон о валюте, ГК, инструкции НБРБ — не надеются на удачный сниппет поисковика. Это правильный ответ на хрупкий веб.
5. **Retrieval не «top-k cosine».** `knowledge_retrieve.py`: multi-query, BM25 + keyword + dense, RRF, heading boost для «Статья N», rerank через Qwen3-Reranker (logprob yes/no), MMR, соседние чанки, merge статей. Для юридического RAG это осмысленный пайплайн.
6. **Промпты вынесены** в `docs/prompts/*.txt` (~31 файл), подставляются без рекурсивного `{placeholder}`. Методолог может править текст, не трогая Python.
7. **Артефакты унифицированы** через `document_artifact.ArtifactSpec` (саммари / total / программа / гипотезы / мнение / заключение). Это правильная абстракция после третьего Word-потока.
8. **Тесты на реальные боли.** `test_download_urls` ловит мусорные URL. `test_brief` / `test_conclusion` проверяют гиперссылки, закладки, гипотезы в заключении. `test_pipe_commands` бьёт по фразам из GUIDE (`вопрос`, `утверждаю гипотезы`, ложный новый кейс, `не утверждаю`). Это не «assert True».
9. **Compose продуман под Windows + Ollama на хосте.** RAG-числа и шаблон едут из env, `ENABLE_PERSISTENT_CONFIG=false` — чтобы старый volume не держал MiniLM. `pipe-seed` склеивает `intent.py` + Pipe и ставит функцию через API, если есть `OPENWEBUI_API_KEY`. Это знание из поля, не из туториала.

---

## 2. Находки

Северность: **P0** — ломает обещание продукта или контур банка; **P1** — будет больно при следующем аудиторе / следующем месяце кода; **P2** — гигиена.

### P0 — чинить до показа банку как «коробку»

#### CR-01. Поиск НПА ходит в DuckDuckGo и Bing напрямую — **закрыто 27.08** (флаг)

Файл: `backend/app/services/npa_search.py`, `_search_engines`.

Было: параллельно `html.duckduckgo.com` и `www.bing.com` с названием акта.

Сейчас: `NPA_WEB_FALLBACK` по умолчанию **выкл.** Остаются SearXNG + формы `pravo.by` / `etalonline.by` / `nbrb.by` + `known_sources`. Вкл. fallback — WARNING в лог, название акта уходит к Microsoft/DDG. Compose: `NPA_WEB_FALLBACK=${NPA_WEB_FALLBACK:-false}`.

Recall без внешних поисковиков может быть ниже — это принятый обмен на закрытый контур по умолчанию.

#### CR-02. API без аутентификации, CORS `*`, порты наружу — **закрыто частично 27.08**

- CORS: `settings.cors_origins` (loopback Open WebUI / lab), `allow_credentials=False`. Не `*`.
- Compose: `127.0.0.1:8100:8100`, `127.0.0.1:3000:8080`; SearXNG **без** `ports`.
- **Не закрыто:** API без shared secret. Open WebUI свой auth не защищает backend; с loopback это один аудитор на машине.

#### CR-03. Редирект обходит allowlist — **закрыто 27.08**

`allowlisted_get`: каждый hop — `host_allowed`, иначе `DisallowedHost`. Download и официальный поиск сайтов не следуют на чужой хост. Тесты: `backend/tests/test_contour.py`.

#### CR-04. RAG не умеет отказать

`knowledge_flow.ask`:

```python
if not evidence:
    evidence = chunks[:top_k]
```

Инвариант продукта: «нет фрагмента — модель отказывается». Код при пустой выборке подсовывает **первые K чанков файла** (часто преамбула кодекса). Модель «цитирует» не то.

Плюс в промпт ask подмешиваются карточки саммари как «ориентир». Ориентир без номера статьи легко становится источником выдуманной статьи.

**Рекомендация.** Пустая evidence → фиксированный отказ, `sources=[]`, HTTP 200 с флагом `refused=true`. Саммари не класть в ask-контекст (или класть без права цитировать номера). Eval из `PLAN.md` v0.1 — не опция, а блокер доверия.

---

### P1 — будет стоить недель, если не резать сейчас

Закрыто 27.08: **CR-05** (intent + тесты фраз), **CR-12** (засев Pipe, нужен API key). Открыты: knowledge-бог, копипаста flow, store, дыра retrieval/API, логи, версии git.

#### CR-05. Pipe — бог-объект на regex — **закрыто 27.08** (остаток: сам regex)

Было: `audit_agent.py` ~1458 строк, ноль тестов, Pipe нельзя импортировать вне Open WebUI.

Сейчас:

- чистые функции (`classify`, `_is_*`, `_parse_*`, `_resolve_approval`) в `seed/openwebui/functions/intent.py` (~530 строк, без httpx);
- HTTP-клей и `class Pipe` в `audit_agent.py` (~1070 строк);
- `backend/tests/test_pipe_commands.py` — фразы из `GUIDE.md` плюс негативы (`какие сроки?`, «проверк» в чате, `не утверждаю`, `скачай` без кейса);
- `seed_pipe.py` склеивает intent в один paste (Open WebUI по-прежнему один файл).

Сузили ложные срабатывания: `скачай` только при живом кейсе и без другой команды; новый кейс только с `Проверка …` / `Новая проверка …`, не «любая фраза с аренда».

Остаток (не P1): это всё ещё NLP на regex. Новый тип проверки не требует расширять `_parse_new_case`, но синонимы команд — да. Не ждать LangGraph.

#### CR-06. `knowledge_flow.py` — 660+ строк, фасады пустые

`knowledge_index.py`, `knowledge_ingest.py`, `knowledge_summarize.py` — реэкспорт из `knowledge_flow`. Читатель думает, что модули разделены. На деле один файл: ingest, chunk, embed, summarize, ask, export.

**Рекомендация.** Реально разрезать: ingest / index / summarize / ask. Фасады уже названы — перенести тела.

#### CR-07. Почти одинаковые `*_flow.py`

`brief_flow`, `total_flow`, `program_flow`, `hypotheses_flow`, `opinion_flow`, `conclusion_flow` — копипаста: stale check → SSE events → `chat_complete` → `save_artifact_meta`. `ArtifactSpec` уже вынес пути; оркестрацию нет.

Риск: правка таймаута/force в одном потоке, забыли в другом.

**Рекомендация.** Один `run_llm_artifact(spec, build_prompt, write_file, extra_stale=...)`. Специфику (гипотезы JSON, шрифт мнения, разделы заключения) оставить в маленьких модулях.

#### CR-08. Файловый store без атомарности и без валидации `case_id`

- `path.write_text(state.model_dump_json())` — при падении процесса `case.json` обрежется.
- `case_id` из URL кладётся в `root / case_id`. Создаваемые id — `uuid.hex[:12]`, но API принимает любой путь, включая `../`.
- `_CASE_LOCKS` только на select/download, растёт безcleanup; `propose` и сборка Word не под lock. Два «саммари» параллельно порвут meta.
- Эмбеддинги в `knowledge_index.json` — раздутый JSON на диск, долгий parse, нет версионирования схемы чанка кроме `embed_model`.

Для одного аудитора на одной машине живёт. На двух вкладках — гонки.

**Рекомендация.** `resolve(case_id)` только `[a-f0-9]{8,12}`. Писать `case.json.tmp` + replace. Lock на все мутации кейса. Индекс: numpy/memmap или хотя бы JSONL по чанку, embeddings отдельно.

#### CR-09. Тестовая дыра в ядре

Есть (и это ценно): extra_titles, download URLs, brief/opinion/conclusion docx, hypotheses, case_context, openwebui payload shape, **фразы Pipe** (`test_pipe_commands`).

Нет:

- `knowledge_retrieve.select_evidence` / `retrieve_for_ask` (самое важное для доверия);
- `chunker.chunk_text` на реальном ГК;
- HTTP API (`TestClient`);
- `ask` refuse path;
- редирект allowlist — `test_contour.py`;
- concurrent `store.save`.

`test_brief.py` — 750+ строк, почти целиком docx. Retrieval — 0.

#### CR-10. Нет наблюдаемости — **закрыто частично 27.08**

Было: `logging` один раз. Стало: `app.logging_setup` (request id, `X-Request-Id`, ASGI-middleware без буфера SSE), `LOG_LEVEL`, логи propose/download/search/rerank/ask. Trail `/ask` пишется в `trail/ask.jsonl` через `store.append_jsonl`.

Не закрыто: метрики времени как отдельный экспорт; request id не прокинут в Ollama payload.

#### CR-11. Версии и git — **закрыто 27.08** (история main не трогали)

API `version` = `app.__version__` = **0.0.1**. `GET /` отдаёт `version`, не `step: 2`. CI на PR режет subject `initial commit` / короче 10 символов.

84 старых коммита в `main` остаются как есть: squash + force-push только по явной команде.

#### CR-12. Засев Pipe руками — **закрыто 27.08** (оговорка: ключ)

Сервис `pipe-seed` при `compose up` склеивает `intent.py` + `audit_agent.py` и POST в `/api/v1/functions` (id `auditor`), включает функцию, пишет Valves. Вручную: `python seed/openwebui/seed_pipe.py --print`.

Оговорка: нужен админский `OPENWEBUI_API_KEY` в корневом `.env`. Первый вход без ключа — Pipe не появится, контейнер выйдет 0 и не будет крутиться вечно. После ключа: `docker compose up -d pipe-seed`.

Расхождение `docs/prompts/pipe_help.txt` и константы `HELP` в Pipe — не закрыто (процесс в `PROMPTS.md` тот же).

---

### P2 — гигиена, не стоппер демо

| ID | Суть |
|---|---|
| CR-13 | `datetime.utcnow()` deprecated; везде naive UTC |
| CR-14 | `GET /cases/{id}/knowledge` вызывает `ingest_library` — GET с побочным эффектом |
| CR-15 | `GET /` светит `data_root`, `searxng_url`, имена моделей |
| CR-16 | `GET /health` возвращает `status: created` — бессмысленно |
| CR-17 | Upload в KB: нет лимита размера, `.bin` проходит в raw |
| CR-18 | `searxng/settings.yml`: `secret_key: "audit-tools-local-change-me-32chars"`, `limiter: false`; порт 8080 больше не публикуется |
| CR-19 | Spoof `X-Forwarded-For: 127.0.0.1` к SearXNG — обход бот-детекта; ок для compose, запах |
| CR-20 | `embed_texts` на 404 `/api/embed` эмбеддит только `texts[0]` |
| CR-21 | `searxng_client.find_best_url` дублирует логику `npa_search.build_search_queries` |
| CR-22 | ~~Нет ruff / mypy / pytest.ini / GitHub Actions~~ — закрыто 27.08: `pyproject.toml`, `.github/workflows/ci.yml` |
| CR-23 | `frontend/` в README, в git файлов нет — мёртвая ссылка |
| CR-24 | Широкий `except Exception` + `# noqa: BLE001` почти в каждом роутере — 502 глотает баги программиста |
| CR-25 | `httpx.AsyncClient` кэш по timeout, не закрывается на shutdown |
| CR-26 | Word-генераторы `conclusion_docx.py` (1255) и `brief_docx.py` (741) — неизбежный ад OOXML, но смешаны вёрстка и домен (toc, гипотезы) |

---

## 3. Архитектура: совпадает ли код с `ARCHITECTURE.md`

В целом **да**. Это сильная сторона репозитория.

Сбылось:

- нет своего SPA как продукта;
- кейс на диске, без Postgres;
- Pipe = фазы в коде, не свободный ReAct;
- `вопрос …` → `/knowledge/ask`, иначе `/chat`;
- промпты снаружи кода;
- Pipe сеется при `up` (если задан `OPENWEBUI_API_KEY`).

Разъехалось:

| Документ обещает | Код делает |
|---|---|
| Поиск только SearXNG | По умолчанию да; DDG/Bing за `NPA_WEB_FALLBACK` |
| Trail file/chunk/url | Только `manifest.json` при download; ask пишет `trail/ask.jsonl` |
| DuckDB / evidence | Нет (и не должно в 0.0.1) — ок |
| Нормализация `## Статья N` перед sync | Нет (план v0.1) |
| Коробка: Pipe при `up` | `pipe-seed` при наличии `OPENWEBUI_API_KEY`; без ключа — руками |
| Defense in depth на download | Allowlist на исходный URL и каждый hop редиректа |

---

## 4. Разбор по слоям

### 4.1 Audit Tool Server

**Структура.** `routers/` тонкие, логика в `services/` — нормально. `models.py` — понятный state machine кейса. `config.py` — все RAG-числа в одном месте, хорошо для тюнинга.

**Слабое место — knowledge.** Индекс JSON с векторами внутри, ingest на GET, ask без refuse, саммари в контексте вопроса.

**Слабое место — документы.** Шесть flow-модулей + два огромных docx. Качество выхода зависит от парсера markdown модели. Тесты это частично ловят (`ensure_all_hypotheses`, toc). Нет золотого «эталонного md → эталонный docx» фикстуры на диске (кроме inline SAMPLE_MD).

**Ollama-клиент.** Нормальный: stream propose, `format=json`, `think: False`, strip `<think>`, rerank через generate+logprobs — инженерно аккуратно. Кэш клиентов и молчаливый fallback rerank (`_rerank_unavailable = True`) надо логировать один раз WARNING.

### 4.2 Pipe

Правильная идея: **не ReAct**. Аудитор пишет естественные фразы, код решает фазу.

Маршрутизация вынесена в `intent.py` и покрыта тестами. Regex остался, но уже: `скачай` не ловится в любом предложении; новый кейс — только явный старт (`Проверка …` / `Новая проверка …`). Синонимы команд (`саммари` / `сводка` / `бриф`) по-прежнему список в коде.

HTTP-клей (`class Pipe`, Valves, вызовы API) — `audit_agent.py` ~1070 строк. Open WebUI принимает один файл: `seed_pipe.py` склеивает paste при засеве.

Кейс живёт в HTML-комментарии `<!--audit-case:hex-->`. Умно и хрупко: новый чат = нет кейса (это честно написано в PLAN). Нет `открой кейс X`.

Valves (`AUDIT_API`, timeout 600/1800) — правильный рычаг админа. Засев их проставляет.

### 4.3 Добыча НПА

Лучший «грязный» модуль репозитория: known_sources → expand official URLs → score (штраф новостям, штраф чужому кодексу по коду документа) → download → reject коротких заглушек.

Тесты покрывают именно это. Сохранить. Внешние поисковики за флагом (CR-01), редирект закрыт (CR-03).

### 4.4 Compose / коробка

Плюс: RAG-шаблон и числа в compose, Ollama на хосте, profile `lab` для мёртвого фронта, `pipe-seed` ставит функцию «Аудитор» через API.

Минус: засев молчит без `OPENWEBUI_API_KEY` (первый админ ещё не создал ключ). `latest` образы (`searxng:latest`, `open-webui:main`) — невоспроизводимая коробка. Для банка пинить digest. Нет корневого `.env.example` (он только в `backend/`). Нет healthcheck у сервисов. `--reload` в prod-команде backend (volume `./backend/app`) — удобно для разработки, опасно как «поставка». Порты 3000/8100 на `127.0.0.1`, SearXNG без publish.

### 4.5 Документация vs код

Документации **много и она живая** — GUIDE, AUDITOR, RAG, PROMPTS. Риск: GUIDE ~1000 строк, дублирует AUDITOR. Для аудитора ок, для ревьюера кода — источник расхождения (версия 0.0.1 vs 0.2.0).

---

## 5. Качество кода (стиль, не архитектура)

Хорошо:

- `from __future__ import annotations`, pydantic v2, pathlib;
- говорящие имена (`usable_url`, `is_usable_npa_page`, `artifact_stale`);
- мало магии в зависимостях (`requirements.txt` короткий и по делу).

Плохо:

- нет единого слоя ошибок (ValueError → 400, всё остальное → 502);
- нет structured logging;
- дубли `find_best_url`;
- naive datetime;
- мёртвые реэкспорты, создающие иллюзию модульности;
- Pipe и backend не разделяют типы кейса (Pipe работает с `dict`).

Это не «Junior dump». Это прототип, который вырос быстрее, чем его резали.

---

## 6. Рекомендации: что делать по порядку

Не делать микросервисы, не писать React, не заводить Postgres «на вырост». Совпадает с `START.md`. Ниже — порядок, который реально поднимает оценку.

### Неделя 1 — не нарушать обещания (P0)

1. ~~Выключить DDG/Bing по умолчанию.~~ `NPA_WEB_FALLBACK=false`.
2. ~~После redirect — `host_allowed(final_url)`.~~ `allowlisted_get`.
3. `ask`: пустая evidence → отказ, без `chunks[:top_k]`. Убрать саммари из ask-контекста или пометить «не цитировать».
4. ~~Compose: `127.0.0.1:8100`, SearXNG без publish. CORS только localhost.~~
5. Валидация `case_id` regex.

### Неделя 2 — доверие к цитате (это и есть v0.1)

6. `eval_rag.py`: 20 вопросов (есть в тексте / нет / номер статьи / перефраз). CI или хотя бы `pytest` + jsonl фикстуры чанков, **без** живой Ollama на unit-уровне; live — отдельная марка `@pytest.mark.live`.
7. Тесты на `select_evidence` с фиктивными эмбеддингами.
8. Trail: `trail/ask.jsonl` — case_id, question, chunk ids, filenames, refused.
9. Нормализация текста `## Статья N` перед sync (уже в плане).

### Неделя 3 — не развалиться

10. ~~30 тестов на фразы Pipe~~ — сделано (`test_pipe_commands.py`).
11. `TestClient` на create → select без Ollama (mock propose).
12. Атомарный `store.save`, lock на brief/ask/download.
13. ~~ruff + pytest в GitHub Actions~~ — `.github/workflows/ci.yml`.
14. Версия API 0.0.1. Коммиты с смыслом — CI на PR; история main не схлопнута.

### Потом, не раньше

- Auth на API (секрет из env).
- Дожать засев: ключ/админ при первом `up`, чтобы Pipe был без ручного шага.
- Пинить digest образов.
- SQLite — когда появится второй пользователь или «открой кейс».
- Данные клиента / DuckDB — как в PLAN v0.4, не раньше стабильной цитаты.

---

## 7. Чего не делать

- Не форкать Open WebUI из-за боли с paste Pipe — `seed_pipe.py` уже склеивает и сеет через API.
- Не заменять файловый store на Postgres в этом квартале.
- Не писать второго агента (LangGraph / native tools как primary).
- Не класть клиентский Excel в Knowledge.
- Не «улучшать» RAG новой vector DB, пока нет eval.
- Не развивать `frontend/`.

---

## 8. Оценка модулей (тепловая карта)

| Модуль | Строк (порядка) | Здоровье | Комментарий |
|---|---|---|---|
| `domains` / `downloader.usable_url` | мало | отлично | Тесты есть, инвариант ясен |
| `known_sources` / `extra_titles` | ~280 | отлично | Доменный кэш, так и надо |
| `npa_search` | ~350 | средне | Скоринг на месте; DDG/Bing за флагом |
| `knowledge_retrieve` | ~400 | хорошо | Алгоритм зрелый, gate + eval 20 вопросов |
| `knowledge_flow` | ~660 | слабо | Бог-файл, ask-fallback |
| `ollama_client` | ~380 | хорошо | Rerank hack оправдан, нужен лог |
| `document_artifact` | ~160 | отлично | Правильный шар |
| `*_flow.py` × 6 | ~2.3k | средне | Шаблон не вынесен |
| `brief_docx` + `conclusion_docx` | ~2k | средне | Тесты спасают |
| `storage` / `http` | мало | слабо | Нет атомарности, GET-ingest |
| `intent.py` | ~530 | хорошо | Чистый classify, тесты фраз из GUIDE |
| `audit_agent.py` | ~1.1k | средне | HTTP-клей; paste склеивает `seed_pipe.py` |
| `seed_pipe.py` | ~260 | хорошо | Засев OWUI API; без ключа — no-op |
| `docs/prompts` | 31 файл | отлично | Держать source of truth |
| тесты | ~2.4k | хорошо | Docx + Pipe intent + retrieve/ask + eval_rag |
| git / CI | — | отлично | Actions: ruff + mypy + pytest; PR режет `initial commit` |

---

## 9. Итог для Coleus

Проект **не выглядит как свалка Jupyter-скриптов**. Есть продукт, инварианты, HITL, локальная модель, осмысленный RAG и документация, с которой можно садиться к аудитору.

Главный риск не «кривой Python», а **разрыв обещания**: отказ без цитаты, audit trail, auth на API. Локальность поиска и hop редиректа закрыты (CR-01…03).

Второй риск — **скорость энтропии** в ядре: `knowledge_flow` большой, flow-модули плодятся копипастой. Intent Pipe нарезан; retrieval/ask покрыты тестами.

Практичная планка «можно показывать СВА»: P0 закрыты, 20 золотых вопросов гоняются, Pipe сеется ключом (`pipe-seed`), не из головы каждый раз.

Планка «можно ставить в банк»: P0 + auth на loopback/internal + пины образов + trail. CI и одна версия API уже есть.
