# Open WebUI: настройка под audit-tools

Админский гайд. Куда нажимать и какие числа ставить, чтобы чат «Аудитор» работал предсказуемо: цитаты из утверждённых актов, без MiniLM, без обрезанного контекста, без веб-поиска в каждом сообщении.

Как аудитору пользоваться чатом: [`GUIDE.md`](GUIDE.md).  
Как один раз вставить Pipe: [`seed/openwebui/AGENT.md`](../seed/openwebui/AGENT.md).  
Как устроен retrieval внутри: [`RAG_для_разработчика.md`](RAG_для_разработчика.md).

Интерфейс может быть на русском или английском. Ищите пункт по смыслу: функции, документы, модели, параметры.

---

## 1. Два контура — не путать

Качество продукта **не** живёт в «голом» чате `qwen3.8:27b`.

```
Аудитор  →  модель «Аудитор» (Pipe)
              →  Audit Tool Server :8100
                   →  Ollama (чат + embeddings)
```

| Что делает аудитор | Кто отвечает | Какие настройки влияют |
|---|---|---|
| Модель **Аудитор**, фразы из GUIDE | Pipe + backend | Valves Pipe, `.env` backend, Ollama на хосте |
| «вопрос …» по актам | Backend RAG (`/knowledge/ask`) | `OLLAMA_EMBED_MODEL`, `OLLAMA_NUM_CTX`, температура 0.1–0.2 **на сервере** |
| Голый `qwen3.8:27b` + Knowledge / `#` | Open WebUI RAG | Admin → Документы, Advanced Params модели |
| Sync `.txt` в Workspace → Знания | Open WebUI Knowledge | API-ключ + те же Документы |

Настройки **Документы** в Open WebUI нужны для опциональной коллекции Knowledge и для отладки голой модели. Продуктовый вопрос по норме идёт через сервер, не через «прикрепить Knowledge к Pipe».

Если крутить только слайдеры в чате `ol`, а Pipe не поставить — аудитора нет.

---

## 2. Перед первым входом

### 2.1. Ollama на хосте (не в Docker)

На Windows: в настройках Ollama включите **Expose Ollama to the network** (или `OLLAMA_HOST=0.0.0.0:11434`). Иначе контейнер Open WebUI не достучится до `host.docker.internal:11434`.

Скачайте модели:

```powershell
ollama pull qwen3.8:27b
ollama pull qwen3-embedding:latest
```

Проверка:

```powershell
ollama list
curl http://localhost:11434/api/tags
```

`qwen3.8:27b` — чат. `qwen3-embedding:latest` — векторы для русского юр. языка. **Не** `all-MiniLM-L6-v2` (дефолт Open WebUI, слабый для НПА РБ).

### 2.2. Стек

```powershell
cd c:\Users\audit\Work\Arina\2026\audit-tools
docker compose up -d --build
```

- продукт: http://localhost:3000
- API: http://localhost:8100/docs
- SearXNG: внутренний, аудитору не открывать

Часть RAG-env из `docker-compose.yml` — **PersistentConfig**: применяется при **первом** создании volume `audit-tools_open-webui-data`. Если volume уже жил со старыми дефолтами — правьте Admin вручную (раздел 4). Сносить volume только если готовы потерять чаты.

---

## 3. Первый вход

1. Откройте http://localhost:3000.
2. Создайте **администратора** (первый пользователь = админ).
3. Язык: имя внизу слева → **Настройки** → интерфейс **Русский**.
4. Имя продукта в шапке должно быть **Аудитор** (`WEBUI_NAME`).

Дальше всё ниже — под админом. Аудитору эти экраны не нужны.

---

## 4. Admin → Настройки → Документы

Путь: имя → **Панель администратора** → **Настройки** → **Документы**  
(англ.: Admin Panel → Settings → Documents; в части версий — Tools → Documents).

После правок — **Сохранить**. Сменили embedding — **Reindex** (только Knowledge, не файлы из чата).

### 4.1. Целевые значения (качество под 32k+ контекст)

| Параметр | Значение | Зачем |
|---|---|---|
| **Движок встраивания** / Embedding engine | **Ollama** | Не SentenceTransformers внутри контейнера |
| **Модель встраивания** | `qwen3-embedding:latest` | Юридический русский; та же, что у backend |
| **URL Ollama для RAG** | `http://host.docker.internal:11434` | Как в compose |
| **Гибридный поиск** | **вкл** | BM25 ловит «ст. 625», «Инструкция 38»; векторы ловят перефраз |
| **Разделитель текста** | **token** | Чанки сопоставимы с окном модели |
| **Markdown Header Splitting** | **вкл** | Если в `.txt` есть `## Статья N` |
| **Размер чанка** | **1600** | Кусок статьи + соседние абзацы |
| **Перекрытие** | **180** | Номер статьи часто в предыдущем абзаце |
| **Chunk Min Size Target** | **1000** | Склеить «Статья 5.» без текста со следующим куском |
| **Top K** | **16** | Сколько кандидатов взять до отсечения |
| **Top K Reranker** | **10** | Сколько реально уйдёт в промпт (если hybrid+rerank) |
| **Relevance threshold** | **0** | Поднимать только когда в цитатах мусор |
| **Full Context** | **выкл** | ГК/НК целиком не влезут; короткую инструкцию можно точечно |
| **Bypass embedding** | **выкл** | Это «засунуть всё», не RAG |
| **RAG в system** (`RAG_SYSTEM_CONTEXT`) | **вкл** | Контекст не прыгает по истории, follow-up быстрее |
| **Веб-поиск в чате** | **выкл** | SearXNG только на backend при download |

Reranker (`BAAI/bge-reranker-v2-m3`) — второй по силе рычаг после embedding. Включайте **только** если есть GPU сверх 27B и доступ к весам (не air-gap). Иначе hybrid без rerank — нормальный прод.

### 4.2. Шаблон RAG

Вставить целиком [`seed/openwebui/RAG_TEMPLATE.txt`](../seed/openwebui/RAG_TEMPLATE.txt). Плейсхолдер `{{CONTEXT}}` **не трогать**. Не добавлять `{{QUERY}}` — вопрос продублируется.

Смысл шаблона: только фрагменты; нет в тексте — отказ; цитата = название акта + статья/пункт + дословно; не подмешивать РФ/ЕС/IFRS; клиентские факты не выдумывать.

Это шаблон **Open WebUI**. Промпт сервера для `вопрос …` живёт в backend (`ASK_SYSTEM`) и уже заточен под аудит. Оба должны говорить одно и то же: не выдумывать нормы.

---

## 5. Admin → Модели: `qwen3.8:27b`

Ollama подхватывается сама, если `OLLAMA_BASE_URL` живой. Если списка нет: Admin → **Настройки** → **Подключения** → Ollama URL `http://host.docker.internal:11434` → проверить соединение.

Откройте модель `qwen3.8:27b` → **Advanced Params** (или «Параметры модели»). Это влияет на **голый** чат с иконкой `ol` и на запасной путь Tools. Pipe «Аудитор» для propose/ask/саммари вызывает Ollama **через backend**, там уже стоят те же числа.

| Параметр | Значение | Если оставить дефолт |
|---|---|---|
| **Context Length** (`num_ctx`) | **32768** | 2048: чанки RAG обрежутся, «модель не видит документ» |
| **Temperature** | **0.2** | Выше 0.5 — вольные формулировки статей |
| **Top P** | **0.9** | |
| **Repeat penalty** | **1.05–1.1** | |
| **Seed** | пусто (или фиксировать для отладки) | |
| **Think / reasoning** | **выкл** | Qwen3 иначе жрёт окно на «размышления» |
| **Function calling** | Default / off для продукта | Native на 27B часто не вызывает tool или качает без «утверждаю» |
| **Vision** | не нужен | |

Математика окна (chunk 1600 × top-k 16 ≈ 25k токенов + история + ответ). На 8k контексте RAG физически не влезет — будут чинить «не ту embedding», хотя виноват `num_ctx`.

Workspace → Модели → **не** создавать вторую модель «Аудитор» на базе Ollama. Имя **Аудитор** должно остаться за Pipe.

---

## 6. Pipe «Аудитор» (главный агент)

Пошагово: [`seed/openwebui/AGENT.md`](../seed/openwebui/AGENT.md). Кратко:

1. http://localhost:3000/admin/functions (не Workspace).
2. **Создать**, ID `auditor` (латиница), название `Аудитор`.
3. Вставить весь [`seed/openwebui/functions/audit_agent.py`](../seed/openwebui/functions/audit_agent.py).
4. Переключатель **Включено**.
5. Шестерёнка → Valves → **Сохранить**.
6. F5 в чате. Новый чат → модель **Аудитор** (иконка **не** `ol`).

### Valves (Параметры функции)

| Valve | Значение | Когда менять |
|---|---|---|
| `AUDIT_API` | `http://backend:8100` | WebUI в Docker. С хоста без compose: `http://localhost:8100` |
| `PUBLIC_API` | `http://localhost:8100` | Ссылки zip/docx в браузере аудитора |
| `TIMEOUT_SEC` | `600` | Propose/download. 27B на списке актов — минуты |
| `BRIEF_TIMEOUT_SEC` | `1800` | Саммари, саммари total, программа в Word, гипотезы в Excel |
| `OPENWEBUI_API_KEY` | пусто **или** ключ | Пусто = ответы только через индекс сервера. Ключ = ещё и коллекция Knowledge |

Ключ: Open WebUI → **Настройки** → **Аккаунт** → **API Keys**. Тот же ключ можно положить в корневой `.env` как `OPENWEBUI_API_KEY=` — его подхватит backend для sync. В git ключ не коммитить.

### Что не вешать на Pipe

- Коллекцию Knowledge (сервер ищет сам).
- Веб-поиск в чате.
- Скрепку с клиентским Excel/договором — норма и факт смешаются.
- Native tools `audit_case.py` — это запасной путь, не продукт.

---

## 7. Что выключить глобально

| Тумблер | Где | Почему |
|---|---|---|
| **Web Search** / веб-поиск | compose `ENABLE_WEB_SEARCH=false` и Admin | Иначе модель гуглит РФ/форумы. Акты качает только backend по allowlist |
| **Community / Arena** модели | Admin → Модели | Посторонние облачные LLM |
| **OpenAI / внешние API** | Admin → Подключения | Клиентский текст не должен уходить с машины |
| **Temporary Chat** для НПА | чат | Парсит файлы на фронте, без нормального экстрактора |
| **Memories** про проверку | Настройки пользователя | Память о пользователе ≠ база НПА |
| **Using Entire Document** на кодексах | файл / Knowledge | Только на короткой инструкции (~15 стр.) |

SearXNG на `:8080` аудитору не нужен. Это внутренний поисковик backend.

---

## 8. Backend — те же рычаги качества

Open WebUI не задаёт температуру propose/ask. Это `backend`:

| Env / код | Значение | Смысл |
|---|---|---|
| `OLLAMA_MODEL` | `qwen3.8:27b` | Propose, карточки, ask, саммари, total, программа, обычный чат |
| `OLLAMA_EMBED_MODEL` | `qwen3-embedding:latest` | Индекс кейса и вопрос |
| `OLLAMA_NUM_CTX` | `32768` | Окно на сервере |
| `OLLAMA_BASE_URL` | в compose: `http://host.docker.internal:11434` | |
| температура propose | `0.2` | `ollama_client.py` |
| температура карточек саммари | `0.1` | меньше вольности в формулировках |
| `rag_top_k` | `12` | сколько чанков сервер кладёт в ask |

Сменили embedding — пересобрать индекс кейса (заново download/ingest), не только Reindex в WebUI.

---

## 9. Чеклист «можно показывать аудитору»

- [ ] Ollama слушает сеть, обе модели в `ollama list`.
- [ ] http://localhost:3000 открывается, имя в шапке **Аудитор**.
- [ ] В Документах embedding = Ollama `qwen3-embedding:latest`, не MiniLM.
- [ ] Hybrid вкл, chunk 1600 / overlap 180, full context выкл.
- [ ] Вставлен `RAG_TEMPLATE.txt`, есть `{{CONTEXT}}`.
- [ ] У `qwen3.8:27b` context length 32768, temperature 0.2.
- [ ] Веб-поиск выключен.
- [ ] Pipe `auditor` включён, Valves `AUDIT_API=http://backend:8100`.
- [ ] Новый чат → в списке моделей **Аудитор** без иконки `ol`.
- [ ] Пробная фраза: `Проверка аренды коммерческой недвижимости, аренда, валюта, НДС` — приходит нумерованный список, **без** скачивания до `утверждаю`.

Золотые тесты RAG (после скачанного акта) — [`RAG_для_разработчика.md`](RAG_для_разработчика.md) §12: пункт есть в тексте; пункта нет — отказ; поиск по «ст. N»; перефраз без номера.

---

## 10. Если отвечает ерунду

| Симптом | Куда смотреть |
|---|---|
| Нет модели **Аудитор** | Functions: ID `auditor`, включено, F5. Не Workspace → Models |
| Список актов пустой / таймаут | Ollama жив? 27B влезает в VRAM? `TIMEOUT_SEC` |
| Цитаты «из головы», блока «Откуда в базе» нет | Пишете в голый `ol`, не в Pipe. Или вопрос без префикса `вопрос` |
| «База знаний пуста» | Нет `утверждаю`, download не закончился |
| Видит не ту статью | Hybrid выкл / MiniLM / маленький top-k / `num_ctx=2048` |
| После смены embedding бред | Нет reindex (OWUI) и нет пересборки `knowledge_index.json` (сервер) |
| `NoneType.encode` | Embedding не загрузилась. Save в Документах, `ollama run qwen3-embedding` |
| Контейнер не видит Ollama | Windows: Expose to network |
| Env из compose «не применился» | Старый volume. Править Admin или удалить `audit-tools_open-webui-data` (сотрёт чаты) |
| CUDA OOM | 27B + embedding на одном GPU. Не эмбеддить во время длинной генерации; уменьшить batch |

Не меняйте чат-модель первой. Сначала extractor (читаемый `.txt`), потом embedding, потом `num_ctx`, потом top-k.

---

## 11. Запасной путь (не для аудитора)

Workspace → Инструменты → [`tools/audit_case.py`](../seed/openwebui/tools/audit_case.py) + system [`SYSTEM_AUDITOR.txt`](../seed/openwebui/SYSTEM_AUDITOR.txt) + Function calling **Native**.

27B часто вызовет `download` без утверждения. Для продукта — только Pipe.

---

## 12. Файлы засева

| Файл | Куда |
|---|---|
| `docker-compose.yml` → `open-webui.environment` | Стартовые RAG-env на чистом volume |
| `seed/openwebui/RAG_TEMPLATE.txt` | Admin → Документы → шаблон |
| `seed/openwebui/functions/audit_agent.py` | Admin → Функции |
| `seed/openwebui/SYSTEM_AUDITOR.txt` | только Tools+Ollama |
| корневой `.env` | `OLLAMA_MODEL`, `OLLAMA_EMBED_MODEL`, `OPENWEBUI_API_KEY` |
