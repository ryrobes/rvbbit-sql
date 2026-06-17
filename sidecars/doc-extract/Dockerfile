FROM python:3.12-slim

# markitdown needs a few system libs for office/pdf/image handling.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libmagic1 libgl1 libglib2.0-0 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

# Mount the shared staging volume at /staging (same path the connector + rvbbit use),
# e.g. docker run -v rvbbit_brain_staging:/staging ...
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
