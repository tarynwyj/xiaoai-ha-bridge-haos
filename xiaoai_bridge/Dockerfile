FROM python:3.12-slim

ARG UPSTREAM_REF=e4c82d4b4e872ff5d02d1baec089a6b4ad36734a
ARG APP_VERSION=dev

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git init /app \
    && cd /app \
    && git remote add origin https://github.com/chenshuhe/xiaoai-ha-bridge.git \
    && git fetch --depth 1 origin "${UPSTREAM_REF}" \
    && git checkout --detach FETCH_HEAD

WORKDIR /app

# Runtime dependencies are pinned so the same patched commit always builds the
# same image (the upstream requirements.txt only declares lower bounds).
COPY requirements.lock.txt /tmp/requirements.lock.txt
RUN pip install --no-cache-dir -r /tmp/requirements.lock.txt \
    && rm /tmp/requirements.lock.txt

COPY patch_web.py /tmp/patch_web.py
RUN python -m py_compile /tmp/patch_web.py \
    && python /tmp/patch_web.py \
    && grep -Fq '<select class="r-e" multiple size="6">' /app/web/index.html \
    && grep -Fq 'function ruleSummaryHtml(rule)' /app/web/index.html \
    && grep -Fq 'async function loadRuleDevices()' /app/web/index.html \
    && grep -Fq 'window.fetch=function(input,init)' /app/web/index.html \
    && grep -Fq 'id="rules-dirty"' /app/web/index.html \
    && grep -Fq 'clearXiaomiLogin' /app/web/index.html \
    && rm /tmp/patch_web.py

COPY patch_auth.py /tmp/patch_auth.py
RUN python -m py_compile /tmp/patch_auth.py \
    && python /tmp/patch_auth.py \
    && python -m py_compile /app/bridge.py \
    && grep -Fq 'async def _refresh_micoapi_token' /app/bridge.py \
    && grep -Fq 'micoapi serviceToken 已保存' /app/bridge.py \
    && grep -Fq 'async def _relogin_and_refresh' /app/bridge.py \
    && grep -Fq 'async def _enforce_access_control' /app/bridge.py \
    && grep -Fq 'async def reveal_direct_token' /app/bridge.py \
    && grep -Fq 'proxy_headers=False' /app/bridge.py \
    && ! grep -Fq 'ssl=False' /app/bridge.py \
    && ! grep -Fq 'str(TOKEN_PATH)' /app/bridge.py \
    && rm /tmp/patch_auth.py

# Build-time regression suite: login/token/credential simulation, HTTP-level
# access control and the HAOS Ingress contract (port, relative fetch paths).
# They only need the runtime dependencies, so a broken patch fails the build
# instead of reaching users.
COPY test_startup_patch.py test_api_auth.py test_ingress_contract.py /tmp/
RUN python /tmp/test_startup_patch.py /app/bridge.py \
    && python /tmp/test_api_auth.py /app \
    && python /tmp/test_ingress_contract.py \
         --bridge /app/bridge.py --html /app/web/index.html --no-node \
    && rm /tmp/test_startup_patch.py /tmp/test_api_auth.py /tmp/test_ingress_contract.py

RUN rm -rf /app/config /app/logs \
    && ln -s /data/config /app/config \
    && ln -s /data/logs /app/logs

EXPOSE 47521

LABEL org.opencontainers.image.source="https://github.com/tarynwyj/xiaoai-ha-bridge-haos" \
      org.opencontainers.image.description="XiaoAI to Home Assistant bridge for HAOS" \
      io.hass.name="XiaoAI HA Bridge" \
      io.hass.description="XiaoAI voice bridge for Home Assistant" \
      io.hass.version="${APP_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="amd64"

CMD ["sh", "-c", "mkdir -p /data/config /data/logs && exec python -u bridge.py"]
