# Audit Tools — API библиотеки НПА и знаний

Продуктовый вход для аудитора — чат Open WebUI, не этот API: [`docs/GUIDE.md`](../docs/GUIDE.md). Ниже — эндпоинты для разработки tools и отладки.

В чате: `вопрос …` → `/knowledge/ask`. Сообщение без префикса → `POST /api/v1/chat` (не RAG).

## Что делает этот шаг

1. Аудитор создаёт кейс: **название проверки** + **ключевые слова**
2. Локальная LLM (Ollama) предлагает список НПА / документов
3. Аудитор валидирует и выбирает документы (`document_ids`) и при необходимости дописывает свои названия (`extra_titles`)
4. Backend ищет URL через **SearXNG + DuckDuckGo + Bing** и **скачивает** только страницы с allowlist доменов РБ

```
data/audit_cases/{case_id}/
  case.json
  manifest.json
  knowledge_raw/      скачанные файлы
  knowledge_text/     очищенный текст
  summaries/          саммари по актам
  totals/             конспект из знаний модели
  programs/           программа проверки
  hypotheses/         чеклист гипотез
  opinions/           раздел I — аудиторское мнение
  reports/            полное аудиторское заключение
  knowledge_index.json
  library.zip
```

Клиентские файлы в поиск **не отправляются**. В запрос уходит название акта.

## Запуск

```powershell
cd c:\Users\audit\Work\Arina\2026\audit-tools\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8100
```

Swagger: http://localhost:8100/docs

Проверки перед пушем (то же гоняет GitHub Actions):

```powershell
pip install -r requirements-dev.txt
cd ..
ruff check backend/app backend/tests
mypy
pytest
```

Нужны запущенные:
- Ollama (`qwen3.8:27b`)
- SearXNG на `http://localhost:8080` с `format=json`

## API flow

### 1. Создать кейс
```http
POST /api/v1/cases
{
  "inspection_name": "Проверка аренды коммерческой недвижимости",
  "keywords": ["аренда", "валютные расчёты", "НДС", "нежилое помещение"]
}
```

### 2. Предложить документы (LLM)
```http
POST /api/v1/cases/{case_id}/propose
GET  /api/v1/cases/{case_id}/propose/stream
```

### 3. Выбрать документы (валидация аудитора)
```http
POST /api/v1/cases/{case_id}/select
{
  "document_ids": ["abc123", "def456"],
  "extra_titles": [
    "Инструкция НБРБ № 38",
    "Положение о внутреннем контроле"
  ]
}
```

### 4. Скачать (поиск URL → allowlist)
```http
POST /api/v1/cases/{case_id}/download
```

### 5. Посмотреть библиотеку
```http
GET /api/v1/cases
GET /api/v1/cases/{case_id}
GET /api/v1/cases/{case_id}/library
GET /api/v1/cases/{case_id}/library/archive
```

### 6. Документы проверки (v0.0.1)
```http
POST /api/v1/cases/{case_id}/knowledge/brief
GET  /api/v1/cases/{case_id}/knowledge/summary.docx
POST /api/v1/cases/{case_id}/knowledge/total
GET  /api/v1/cases/{case_id}/knowledge/total.docx
POST /api/v1/cases/{case_id}/knowledge/program
GET  /api/v1/cases/{case_id}/knowledge/program.docx
POST /api/v1/cases/{case_id}/knowledge/hypotheses
GET  /api/v1/cases/{case_id}/knowledge/hypotheses.xlsx
POST /api/v1/cases/{case_id}/knowledge/hypotheses/select
POST /api/v1/cases/{case_id}/knowledge/opinion
GET  /api/v1/cases/{case_id}/knowledge/opinion.docx
POST /api/v1/cases/{case_id}/knowledge/conclusion
GET  /api/v1/cases/{case_id}/knowledge/conclusion.docx
```

Стрим прогресса: `GET …/brief/stream`, `GET …/total/stream`, `GET …/program/stream`, `GET …/hypotheses/stream`, `GET …/opinion/stream`, `GET …/conclusion/stream`. `force=true` — пересобрать. Мнение и заключение: `font=c` (Calibri) или `font=t` (Times New Roman).

Как устроен `саммари` (карточка акта, map-reduce, большая библиотека): [`docs/SAMMARI.md`](../docs/SAMMARI.md).

`саммари total` — краткий конспект по теме из знаний модели (без опоры на скачанные акты), со ссылками на акты и статьи.

`гипотезы` — Excel-чеклист 8–10 гипотез для проверки. Лучше, если уже есть саммари / total / программа.

`аудиторское мнение` — черновик раздела I заключения (2–4 стр., без таблиц) из **подтверждённых** гипотез. Сначала `утверждаю гипотезы 1, 3, 5`. Шрифт: `-c` / `font=c` Calibri, `-t` / `font=t` Times New Roman.

`аудиторское заключение` — полный черновик заключения в Word: титул, содержание, раздел I из уже собранного мнения, наблюдения по шаблону, общая информация. Раздел II не пишется. Нужны подтверждённые гипотезы и готовое `аудиторское мнение`. Шрифт тот же: `-c` / `-t`.

Обычный диалог без RAG:

```http
POST /api/v1/chat
{ "messages": [ { "role": "user", "content": "…" } ] }
```

Индекс после скачивания: `POST …/knowledge/index`. Загрузка файла акта (не Excel клиента): `POST …/knowledge/upload`.

Дополнительно для tools/отладки:

```http
GET  /health
GET  /api/v1/cases/{case_id}/knowledge
POST /api/v1/cases/{case_id}/knowledge/ingest
GET  /api/v1/cases/{case_id}/knowledge/build/stream
GET  /api/v1/cases/{case_id}/knowledge/export
GET  /api/v1/knowledge/openwebui/status
POST /api/v1/cases/{case_id}/knowledge/openwebui/sync
```

## Allowlist доменов

- pravo.gov.by / pravo.by / etalonline.by
- nbrb.by
- minfin.gov.by
- nalog.gov.by
- government.by
- president.gov.by

## Как ищем URL

Поисковики часто банят SearXNG (403 / suspended), и одного allowlist-индекса мало: без DDG/Bing часть актов на `pravo.by` не находится. Download идёт так:

1. `manual_urls` из `/select` (если аудитор указал ссылку)
2. curated `known_sources.py` (официальные URL по названию акта)
3. параллельный поиск: SearXNG + DuckDuckGo HTML + Bing; в кандидаты попадают только URL с allowlist выше

На диск качается страница с `pravo.by` / `etalonline.by` / `nbrb.by` (и остальной allowlist). Сам запрос при этом уходит во внешние поисковики — это не air-gap.

Пример select с ручной ссылкой:

```json
{
  "document_ids": ["abc123"],
  "manual_urls": {
    "abc123": "https://pravo.by/document/?guid=..."
  }
}
```

## Пример PowerShell

```powershell
$base = "http://localhost:8100/api/v1"
$case = Invoke-RestMethod -Method Post -Uri "$base/cases" -ContentType "application/json" -Body (@{
  inspection_name = "Проверка аренды коммерческой недвижимости"
  keywords = @("аренда","валюта","НДС")
} | ConvertTo-Json)

$id = $case.case_id
$prop = Invoke-RestMethod -Method Post -Uri "$base/cases/$id/propose"
$prop.documents | Select-Object id, title, priority | Format-Table

# выбрать все priority=1
$ids = @($prop.documents | Where-Object { $_.priority -eq 1 } | ForEach-Object { $_.id })
Invoke-RestMethod -Method Post -Uri "$base/cases/$id/select" -ContentType "application/json" -Body (@{ document_ids = $ids } | ConvertTo-Json)

Invoke-RestMethod -Method Post -Uri "$base/cases/$id/download"
Invoke-RestMethod -Uri "$base/cases/$id/library"
```
