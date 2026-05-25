FROM {{ base_image }}

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV HF_HOME=/root/.cache/huggingface
ENV TRANSFORMERS_CACHE=/root/.cache/huggingface
ENV RVBBIT_CAPABILITY_MODEL="{{ model }}"
ENV RVBBIT_CAPABILITY_REVISION="{{ revision }}"
ENV RVBBIT_CAPABILITY_HANDLER="{{ handler }}"
ENV RVBBIT_CAPABILITY_DEVICE="{{ device }}"

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
