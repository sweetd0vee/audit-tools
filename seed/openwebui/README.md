# Засев Open WebUI

Исходники мнения продукта. Не форк WebUI: эти тексты вставляются в Admin (и частично едут в `docker-compose.yml` как env).

| Файл | Куда |
|---|---|
| `SYSTEM_AUDITOR.txt` | только если цепляете Tools к Ollama-модели, не к Pipe |
| `RAG_TEMPLATE.txt` | Admin → Settings → Documents → RAG template. Плейсхолдер `{{CONTEXT}}` обязателен |
| `functions/audit_agent.py` | **главный агент.** Admin → Functions. Как ставить: [AGENT.md](AGENT.md). Как пользоваться: [GUIDE.md](../../docs/GUIDE.md) |
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
5. Агент: Admin → Functions, вставить `functions/audit_agent.py` — см. [AGENT.md](AGENT.md). Сценарий аудитора: [`docs/GUIDE.md`](../../docs/GUIDE.md).
6. Веб-поиск в чате **выключен**. SearXNG только для добычи актов через backend.

`RAG_*` в compose — PersistentConfig: срабатывают при **первом** создании volume. Если `open-webui-data` уже был — правьте Admin вручную или `docker compose down` и удалите volume (сотрёт чаты).

Золотые тесты: [`docs/RAG_для_разработчика.md`](../../docs/RAG_для_разработчика.md) §12.
