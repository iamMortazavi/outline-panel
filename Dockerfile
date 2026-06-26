FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/outline_bot.db \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# DB lives on a volume so it survives container rebuilds
VOLUME ["/data"]
EXPOSE 8000

# Default: run the web dashboard. Override `command` to run the bot
# (`outline-panel-bot`) — see docker-compose.yml.
CMD ["outline-panel"]
