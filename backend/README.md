# Audit Tools — Step 1: библиотека НПА

Продуктовый вход для аудитора — чат Open WebUI, не этот API: [`docs/AUDITOR.md`](../docs/AUDITOR.md). Ниже — эндпоинты для разработки tools.

## Что делает этот шаг

1. Аудитор создаёт кейс: **название проверки** + **ключевые слова**
2. Локальная LLM (Ollama) предлагает список НПА / документов
3. Аудитор валидирует и выбирает документы (`document_ids`)
4. Backend ищет через **SearXNG** (только allowlist доменов РБ) и **скачивает** в папку кейса

```
data/audit_cases/{case_id}/
  case.json
  manifest.json
  knowledge_raw/   ← скачанные файлы
```

Клиентские данные в поиск **не отправляются**.

## Запуск

```powershell
cd c:\Users\audit\Work\test\audit-tools\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8100
```

Swagger: http://localhost:8100/docs

Нужны запущенные:
- Ollama (`qwen3.6:35b`)
- SearXNG на `http://localhost:8080` с `format=json`

## API flow

### 1. Создать кейс
```http
POST /api/v1/cases
{
  "inspection_name": "Проверка аренды коммерческой недвижимости",
  "keywords": ["аренда", "валютные расчёты", "НДС", "нежилое помещение"],
  "period": "2025"
}
```

### 2. Предложить документы (LLM)
```http
POST /api/v1/cases/{case_id}/propose
```

### 3. Выбрать документы (валидация аудитора)
```http
POST /api/v1/cases/{case_id}/select
{
  "document_ids": ["abc123", "def456"]
}
```

### 4. Скачать через SearXNG
```http
POST /api/v1/cases/{case_id}/download
```

### 5. Посмотреть библиотеку
```http
GET /api/v1/cases/{case_id}/library
```

## Allowlist доменов

- pravo.gov.by / pravo.by / etalonline.by
- nbrb.by
- minfin.gov.by
- nalog.gov.by
- government.by
- president.gov.by

## Fallback, если SearXNG пустой

Поисковики часто банят SearXNG (403 / suspended). Поэтому download идёт так:

1. `manual_urls` из `/select` (если аудитор указал ссылку)
2. curated `known_sources.py` (официальные URL по названию акта)
3. SearXNG (allowlist)

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
  period = "2025"
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
