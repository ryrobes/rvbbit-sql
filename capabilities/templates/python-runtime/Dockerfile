FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py runner.py ./

ENV RVBBIT_PYTHON_ENVS_DIR=/var/lib/rvbbit/python-envs
ENV RVBBIT_PYTHON_HANDLERS_DIR=/var/lib/rvbbit/python-handlers

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
