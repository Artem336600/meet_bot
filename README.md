meet-bot

Минимальный каркас FastAPI + Postgres (+Redis опционально).

Запуск:
- Скопируйте `.env.example` в `.env` при необходимости
- Выполните: `docker compose up -d`
- Проверьте: откройте `http://localhost:8000/health` → ожидаемый ответ `{ "status": "ok" }`

Сервисы:
- app: Python 3.11, FastAPI, Uvicorn (порт 8000)
- db: Postgres 16 (порт 5432)
- redis: Redis 7 (порт 6379, опционально)

Структура:
/app
  /calendar
  /bot
  /db
  /tasks
  main.py
/migrations
docker-compose.yml
requirements.txt
.env.example
README.md


