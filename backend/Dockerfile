# arm64-compatible: the Pi is aarch64.
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq5 \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --no-cache-dir . \
 && apt-get purge -y build-essential && apt-get autoremove -y

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

# Copied with its mode already set, and BEFORE dropping privileges:
# a `RUN chmod` here would run as nnf, which cannot write to
# /usr/local/bin, and the build fails.
COPY --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh

RUN useradd -m -u 1000 nnf && mkdir -p /data/receipts && chown -R nnf /data /app
USER nnf

EXPOSE 8081

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8081"]
