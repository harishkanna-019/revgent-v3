# Multi-stage build: small final image, no build toolchain in runtime
FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Build deps only needed for trafilatura's C extensions (lxml, charset_normalizer)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libxml2-dev \
    libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --user --no-warn-script-location -r requirements.txt


# ─── Runtime image ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/home/app/.local/bin:$PATH

# Runtime libs only (no compiler). libxml2/libxslt for trafilatura.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 \
    libxslt1.1 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/false app

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder --chown=app:app /root/.local /home/app/.local

# Copy application code
COPY --chown=app:app . .

USER app

# Tune defaults for Railway 512 MB plan
ENV LLM_CONCURRENCY=12 \
    SEARCH_CONCURRENCY=8 \
    SCRAPE_CONCURRENCY=4 \
    ABSOLUTE_MAX_COST=5.0 \
    PORT=8000

EXPOSE 8000

# Single worker, single event loop. uvicorn manages async concurrency.
# Increase workers only if Railway plan has multiple CPUs allocated.
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT}"]
