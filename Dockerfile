# Agora hub — server image (issue #58).
# Runs the web UI / REST server. The hub itself is just a directory, mounted at
# /data, so this container is an *additional* easy way to run the server — the
# shared-filesystem workflow (agents connecting via `hubcli listen`) is unchanged.
FROM python:3.12-slim

# Don't write .pyc, unbuffered logs (so `docker logs` is live).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENT_HUB_ROOT=/data \
    AGORA_PORT=8910

WORKDIR /app

# Install deps first (better layer caching), then the package.
COPY pyproject.toml requirements.txt ./
COPY agenthub ./agenthub
RUN pip install --no-cache-dir .

# Hub data lives on a volume so it persists / can be shared.
VOLUME ["/data"]
EXPOSE 8910

COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Healthcheck hits the server's /api/health.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
url='http://127.0.0.1:%s/api/health'%os.environ.get('AGORA_PORT','8910'); \
sys.exit(0 if urllib.request.urlopen(url,timeout=3).status==200 else 1)" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
