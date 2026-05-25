FROM python:3.12-slim

# Install CPU-only torch separately so we don't pull the GPU wheels.
# Swap --extra-index-url to https://download.pytorch.org/whl/cu121 for CUDA.
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r requirements.txt

COPY main.py .

# Models are downloaded on first request (~268MB for distilbert-sst2).
# To bake the model into the image instead, uncomment:
# RUN python -c "from transformers import pipeline; pipeline('sentiment-analysis', model='distilbert-base-uncased-finetuned-sst-2-english')"

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
