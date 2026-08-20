# Tools (ещё не подключены)

Сюда пойдёт `audit_case.py`: тонкие HTTP-обёртки над уже существующим API, без своего UI.

Эндпоинты: `POST /api/v1/cases`, `.../propose`, `.../select`, `.../download`, `GET /api/v1/cases/{id}`, `POST /api/v1/cases/{id}/knowledge/openwebui/sync`.

Пока файл не написан — поток библиотеки только через Swagger или лабораторный `docker compose --profile lab up`.
