FROM python:3.12-slim

ARG UPSTREAM_REF=master

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${UPSTREAM_REF}" \
    https://github.com/chenshuhe/xiaoai-ha-bridge.git /app

WORKDIR /app

COPY patch_web.py /tmp/patch_web.py
RUN python /tmp/patch_web.py && rm /tmp/patch_web.py

RUN pip install --no-cache-dir -r requirements.txt \
    && rm -rf /app/config /app/logs \
    && ln -s /data/config /app/config \
    && ln -s /data/logs /app/logs

EXPOSE 47521

LABEL org.opencontainers.image.source="https://github.com/tarynwyj/xiaoai-ha-bridge-haos" \
      org.opencontainers.image.description="XiaoAI to Home Assistant bridge for HAOS" \
      io.hass.name="XiaoAI HA Bridge" \
      io.hass.description="XiaoAI voice bridge for Home Assistant" \
      io.hass.version="1.0.2" \
      io.hass.type="app" \
      io.hass.arch="amd64"

CMD ["sh", "-c", "mkdir -p /data/config /data/logs && exec python -u bridge.py"]
