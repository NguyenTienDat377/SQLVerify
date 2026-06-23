FROM python:3.13-slim

# z3-solver links against system libstdc++ — ensure it's present
RUN apt-get update && apt-get install -y --no-install-recommends \
        libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps before copying source so this layer is cached on code-only changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render injects $PORT at runtime (default 10000)
ENV PORT=10000
EXPOSE $PORT

# Non-root user — Render runs as root by default but this is safer
RUN useradd --create-home appuser
USER appuser

# --forwarded-allow-ips='*': trust Render's edge proxy X-Forwarded-For so
# request.client.host is the real client IP, not the proxy. Without it every
# request shares one IP and the per-IP rate limits collapse to a global bucket.
CMD uvicorn main:app --host 0.0.0.0 --port $PORT --forwarded-allow-ips='*' --no-server-header
