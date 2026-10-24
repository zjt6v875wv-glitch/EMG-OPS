FROM python:3.12-slim
WORKDIR /app
COPY engine/pyproject.toml engine/pyproject.toml
COPY engine/emg engine/emg
COPY db db
RUN pip install --no-cache-dir ./engine[s3]
WORKDIR /app/engine
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "python -m emg.cli migrate && uvicorn emg.api.app:app --host 0.0.0.0 --port 8000"]
