# GPU-capable embeddings sidecar.
# Base: PyTorch + CUDA 12.4 runtime (~5GB image, but BGE-M3 weights
# download lazily at first call). Falls back to CPU if no GPU mapped
# in.
FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

# HF model cache lives here; mount a volume to persist across rebuilds.
ENV HF_HOME=/root/.cache/huggingface
ENV TRANSFORMERS_CACHE=/root/.cache/huggingface

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
