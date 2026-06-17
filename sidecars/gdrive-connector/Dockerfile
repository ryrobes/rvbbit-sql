FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

# Mount the SHARED staging volume at /staging (same volume the doc-extract sidecar reads):
#   docker run -v rvbbit_brain_staging:/staging -e GDRIVE_SA_KEY=/run/secrets/sa.json ...
# The service account key is provided via GDRIVE_SA_KEY (path or inline JSON); the
# admin shares the target Drive folders with the SA's email.
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
