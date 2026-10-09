# agy/deck v3 — Dockerfile for Hugging Face Spaces (Docker SDK, free tier)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=7860

WORKDIR /app

# system deps for the agy installer
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# agy CLI (official installer)
RUN curl -fsSL https://antigravity.google/cli/install.sh -o /tmp/agy-install.sh \
    && bash /tmp/agy-install.sh -d /usr/local/bin \
    && rm -f /tmp/agy-install.sh \
    && /usr/local/bin/agy --version

COPY . .

EXPOSE 7860

# HF sets PORT=7860; token & keys come from Space secrets:
#   AGY_WEB_TOKEN   - your login token (you choose the value)
#   GEMINI_API_KEY  - your own Google AI Studio key
CMD ["sh", "-c", "exec uvicorn server.main:app --host 0.0.0.0 --port ${PORT:-7860} --log-level warning"]
