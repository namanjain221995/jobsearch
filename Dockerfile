# Backend image: FastAPI API, arq worker and Alembic migrations (the command decides which one runs).
FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /srv

COPY requirements.txt .
# libatomic1: needed by Playwright's bundled Node driver on arm64 slim images.
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 \
    && pip install -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

RUN useradd --create-home --uid 10001 app && chown -R app:app /srv /ms-playwright
USER app
EXPOSE 8010
CMD ["uvicorn", "app.portal.app:app", "--host", "0.0.0.0", "--port", "8010", "--no-access-log"]
