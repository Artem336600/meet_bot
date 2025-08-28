FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg unzip ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV FFMPEG_BINARY=/usr/bin/ffmpeg
ENV PORT=8000

RUN chmod +x ./start.sh || true

CMD ["bash", "-lc", "./start.sh"]


