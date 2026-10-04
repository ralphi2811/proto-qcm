# syntax=docker/dockerfile:1

# ---- dépendances Python (uv) ------------------------------------------------
FROM python:3.12-slim AS deps
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ---- image finale -----------------------------------------------------------
FROM python:3.12-slim
# pango/harfbuzz : rendu PDF WeasyPrint ; fontconfig (fc-match) + DejaVu : la mise en page
# mesure le texte avec la police réellement utilisée dans le PDF
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fontconfig fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
COPY app ./app
COPY static ./static
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 WEB_CONCURRENCY=2
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"
# WEB_CONCURRENCY : nombre de processus uvicorn (rendu PDF et OMR sont gourmands en CPU).
# --proxy-headers : derrière cloudflared / un reverse proxy (schéma https d'origine).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
