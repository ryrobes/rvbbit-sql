FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RVBBIT_MIRROR_STATE_DIR=/var/lib/rvbbit/dlt \
    LOAD__TRUNCATE_STAGING_DATASET=true

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates libpq5 libgssapi-krb5-2 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --requirement /app/requirements.txt

COPY main.py /app/main.py
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin rvbbit && \
    mkdir -p /var/lib/rvbbit/dlt && \
    chown -R rvbbit:rvbbit /app /var/lib/rvbbit/dlt && \
    chmod 0700 /var/lib/rvbbit/dlt

EXPOSE 9182
HEALTHCHECK --interval=10s --timeout=3s --retries=6 \
    CMD python -c "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:9182/health')); raise SystemExit(0 if r.get('ok') else 1)"
USER 10001:10001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9182", "--log-level", "info"]
