FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first so they stay cached across source changes.
COPY pyproject.toml README.md ./
COPY ytcollector/ ./ytcollector/
RUN pip install --no-cache-dir .

# Runtime assets: study configuration and the public Twitch datasets.
COPY config/ ./config/
COPY data/external/ ./data/external/
COPY alembic.ini ./
COPY migrations/ ./migrations/

RUN useradd --create-home --uid 10001 ytc && chown -R ytc:ytc /app
USER ytc

# The container runs one collection and exits. A process that never returns
# blocks every following Railway cron run.
CMD ["sh", "-c", "alembic upgrade head && ytc daily"]
