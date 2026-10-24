FROM python:3.12-slim
WORKDIR /app
COPY engine/pyproject.toml engine/pyproject.toml
COPY engine/emg engine/emg
COPY db db
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir ./engine[s3] \
 # Headless Chromium for vendor portal connectors
 && python -m playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*
WORKDIR /app/engine
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "python -m emg.cli migrate && uvicorn emg.api.app:app --host 0.0.0.0 --port 8000"]
