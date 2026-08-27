# Засев Open WebUI

Исходники мнения продукта. Не форк WebUI: эти тексты вставляются в Admin (и частично едут в `docker-compose.yml` как env).

Полный админский гайд (что включить, какие числа, что выключить): [`docs/OPENWEBUI.md`](../../docs/OPENWEBUI.md).

| Файл | Куда |
|---|---|
| `SYSTEM_AUDITOR.txt` | только если цепляете Tools к Ollama-модели, не к Pipe |
| `RAG_TEMPLATE.txt` | Admin → Settings → Documents → RAG template. Плейсхолдер `{{CONTEXT}}` обязателен |
| `functions/audit_agent.py` + `functions/intent.py` | **главный агент.** `compose up` засевает через `seed_pipe.py`. Вручную: [AGENT.md](AGENT.md) |
| `seed_pipe.py` | Склеивает intent+Pipe в один paste и POST в Admin → Functions |
| `tools/audit_case.py` | запасной путь: Workspace → Tools + native FC |

## После `docker compose up`

1. Откройте **http://localhost:3000**, создайте админа.
2. Admin → Settings → Documents:
   - Embedding engine: **Ollama**, модель `qwen3-embedding:latest` (не MiniLM).
   - Hybrid search: on.
   - Chunk ~1600, overlap ~180, top-k ~16.
   - Full context: off.
   - Вставьте `RAG_TEMPLATE.txt`, если шаблон пустой или дефолтный.
3. Модель чата → Advanced: **context length 32k+**, не 2048.
4. Function calling: Native, если используете Tools; Pipe «Аудитор» tools не нужны.
5. Агент: сервис `pipe-seed` ставит Pipe при `compose up`, если в `.env` есть `OPENWEBUI_API_KEY`. Иначе — [AGENT.md](AGENT.md). Сценарий аудитора: [`docs/GUIDE.md`](../../docs/GUIDE.md).
6. Веб-поиск в чате **выключен**. SearXNG только для добычи актов через backend.

`RAG_*` в compose применяются **каждый** старт (`ENABLE_PERSISTENT_CONFIG=false`): hybrid, qwen embedding, top-k 16, шаблон. MiniLM и `top-k=3` со старого volume больше не живут. Context length модели в Admin всё ещё проверьте: **32k+**, не 2048 (это не env).

Золотые тесты: [`docs/RAG_для_разработчика.md`](../../docs/RAG_для_разработчика.md) §12.
