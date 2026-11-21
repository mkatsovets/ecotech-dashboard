# Pinned to Debian 12 (bookworm): V/AM custom containers only accept images built on
# OS templates the platform supports; the floating `slim` tag moved to a newer Debian.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ECOTECH_DB=/data/ecotech.db \
    PORT=80

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config ./config
COPY data/seed ./data/seed
COPY start.sh ./start.sh
RUN sed -i 's/\r$//' /app/start.sh && chmod +x /app/start.sh

# /data is the persistent volume (SQLite lives there). The container runs as root on purpose:
# platform-mounted volumes are root-owned, and a non-root user could not write the database.
RUN mkdir -p /data
VOLUME /data

# Port 80: the V/AM shared load balancer forwards HTTP to port 80 of a custom container.
EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','80'), timeout=4)"

# Behind the platform load balancer: start.sh trusts X-Forwarded-* headers.
CMD ["/app/start.sh"]
