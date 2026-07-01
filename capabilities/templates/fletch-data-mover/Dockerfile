FROM golang:1.25-bookworm AS fletch-builder

ARG FLETCH_VERSION=v0.2.0

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --depth 1 --branch "${FLETCH_VERSION}" https://github.com/early-signal-tech/fletch.git .
RUN go build -o /out/fletch .

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV RVBBIT_FLETCH_DATA_DIR=/var/lib/rvbbit/fletch
ENV RVBBIT_FLETCH_ALLOW_DRIVER_INSTALL=true
ENV RVBBIT_FLETCH_ALLOW_CUSTOM_DRIVERS=false
ENV ADBC_DRIVER_PATH=/usr/lib/x86_64-linux-gnu:/usr/local/lib

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl lsb-release wget libpq5 && \
    wget -q "https://apache.jfrog.io/artifactory/arrow/$(lsb_release --id --short | tr 'A-Z' 'a-z')/apache-arrow-apt-source-latest-$(lsb_release --codename --short).deb" && \
    apt-get install -y -V "./apache-arrow-apt-source-latest-$(lsb_release --codename --short).deb" && \
    rm "./apache-arrow-apt-source-latest-$(lsb_release --codename --short).deb" && \
    apt-get update && apt-get install -y --no-install-recommends \
        libadbc-driver-postgresql-dev \
        libadbc-driver-sqlite-dev \
        libadbc-driver-flightsql-dev \
        libadbc-driver-snowflake-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=fletch-builder /out/fletch /usr/local/bin/fletch
COPY main.py driver_registry.json /app/

EXPOSE 9181
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9181/health').read()" || exit 1
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9181", "--log-level", "info"]
