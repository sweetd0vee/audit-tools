# Tools

[`audit_case.py`](audit_case.py) — руки для native function calling. Главный агент — Pipe: [`../functions/audit_agent.py`](../functions/audit_agent.py), инструкция: [`../AGENT.md`](../AGENT.md).

Workspace → Tools → вставить файл → привязать к Ollama-модели. 27B часто ломает HITL; для продукта используйте Pipe.

Valves: `AUDIT_API` — URL backend из контейнера (`http://backend:8100`), `PUBLIC_API` — URL ссылок для браузера аудитора (`http://localhost:8100`). После `download_npa` вызывайте `index_knowledge`; `sync_knowledge` нужен только для Open WebUI Knowledge.
