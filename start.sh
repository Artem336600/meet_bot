#!/usr/bin/env bash
set -euo pipefail

# 1) Google OAuth JSON → файл
if [ -n "${GOOGLE_CREDENTIALS_JSON:-}" ]; then
  echo "$GOOGLE_CREDENTIALS_JSON" > /app/credentials.json
  export GOOGLE_APPLICATION_CREDENTIALS=/app/credentials.json
fi

# 2) Vosk модель (ленивая установка)
export VOSK_MODEL_PATH="${VOSK_MODEL_PATH:-/app/models/vosk-model-small-ru-0.22}"
if [ ! -d "$VOSK_MODEL_PATH" ]; then
  mkdir -p /app/models
  cd /app/models
  echo "Downloading Vosk RU small model..."
  curl -L -o vosk-model-small-ru-0.22.zip https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip
  unzip -q vosk-model-small-ru-0.22.zip
  rm vosk-model-small-ru-0.22.zip
  cd /app
fi

# 3) Запуск приложения
# 3) Применим миграции и запустим приложение
alembic upgrade head || true
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"


