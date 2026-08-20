# Audit Tools

Локальный инструмент для аудиторских проверок банка (РБ).

## Step 1 (готово) — библиотека НПА

`backend/` — FastAPI:
- propose (Ollama)
- select (валидация аудитора)
- download (SearXNG → папка кейса)

См. `backend/README.md`.

## Frontend

Минимальный React UI (Vite) для шага 1.

```powershell
# backend (если не запущен)
cd c:\Users\audit\Work\test\audit-tools\backend
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8100

# frontend
cd c:\Users\audit\Work\test\audit-tools\frontend
npm install
npm run dev
```

UI: http://localhost:5174  
API proxy: `/api` → `http://127.0.0.1:8100`
