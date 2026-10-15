# PS PKG Server — self-contained image for docker compose distribution.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ICON_DIR=/data/icons \
    ENTITLEMENTS_CSV=/data/entitlements_all.csv \
    SCAN_WORKERS=8 \
    # Prefetch blocks are allocated in worker threads; a single malloc arena lets
    # the proxy's malloc_trim calls return evicted cache memory to the OS.
    MALLOC_ARENA_MAX=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY app.py ./
COPY pkgtool/ ./pkgtool/

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
