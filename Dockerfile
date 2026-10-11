# Production image for Cloud Run. Local development does not need it.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY db db
COPY scripts scripts

RUN useradd --uid 10001 web
USER web
# Cloud Run sets PORT and terminates HTTPS in front of the container.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'
