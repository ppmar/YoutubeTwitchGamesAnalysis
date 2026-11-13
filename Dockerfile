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

# One image, two roles, picked by a variable so neither service needs its own
# build settings: the cron service leaves YTC_ROLE unset and runs one collection
# then exits (a process that never returns blocks every following cron run); the
# web service sets YTC_ROLE=serve and stays up.
CMD ["sh", "-c", "alembic upgrade head && ytc ${YTC_ROLE:-daily}"]
