FROM python:3.13.14-slim-bookworm

RUN useradd --create-home --uid 10002 bridge \
 && mkdir -p /app/src /workspace /var/lib/codex /canary \
 && chown -R bridge:bridge /app /workspace /var/lib/codex

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --requirement /app/requirements.txt
COPY src /app/src

ENV PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/bridge \
    CODEX_HOME=/var/lib/codex \
    CODEX_BRIDGE_HOST=0.0.0.0 \
    CODEX_BRIDGE_PORT=8787 \
    CODEX_BRIDGE_WORKSPACE=/workspace

USER bridge
CMD ["python", "-m", "supermemory_codex_bridge", "serve"]
