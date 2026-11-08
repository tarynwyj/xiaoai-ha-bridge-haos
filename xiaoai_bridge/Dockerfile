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
    && grep -Fq 'type="checkbox" class="r-e"' /app/web/index.html \
    && grep -Fq 'function saveOneRule(' /app/web/index.html \
    && grep -Fq '保存全部' /app/web/index.html \
    && ! grep -Fq 'multiple size="6"' /app/web/index.html \
    && grep -Fq 'function ruleSummaryHtml(rule)' /app/web/index.html \
    && grep -Fq 'async function loadRuleDevices()' /app/web/index.html \
    && grep -Fq 'window.fetch=function(input,init)' /app/web/index.html \
    && grep -Fq 'id="rules-dirty"' /app/web/index.html \
    && grep -Fq 'clearXiaomiLogin' /app/web/index.html \
    && grep -Fq 'id="mi-otp-panel"' /app/web/index.html \
    && grep -Fq 'id="mi-advanced-card"' /app/web/index.html \
    && grep -Fq 'async function startPasswordLogin()' /app/web/index.html \
    && grep -Fq 'async function verifyCookieLogin()' /app/web/index.html \
    && ! grep -Fq '免短信' /app/web/index.html \
    && grep -Fq 'api/logs?n=500' /app/web/index.html \
    && rm /tmp/patch_web.py

COPY patch_auth.py /tmp/patch_auth.py
RUN python -m py_compile /tmp/patch_auth.py \
    && python /tmp/patch_auth.py \
    && python -m py_compile /app/bridge.py \
    && grep -Fq 'async def _refresh_micoapi_token' /app/bridge.py \
    && grep -Fq 'micoapi serviceToken 已保存' /app/bridge.py \
    && grep -Fq 'async def _relogin_and_refresh' /app/bridge.py \
    && grep -Fq 'class MicoapiLoginRejected' /app/bridge.py \
    && grep -Fq 'async def _relogin_retry_plan' /app/bridge.py \
    && grep -Fq 'def _refresh_interval_seconds' /app/bridge.py \
    && grep -Fq 'def _token_age_seconds' /app/bridge.py \
    && grep -Fq 'def _log_file_handler' /app/bridge.py \
    && grep -Fq 'def _poll_targets' /app/bridge.py \
    && grep -Fq 'bridge_state["poll_cursor"]' /app/bridge.py \
    && ! grep -Fq 'device_id = devices[0].get("deviceID", "")' /app/bridge.py \
    && grep -Fq 'def _log_lines_from_file' /app/bridge.py \
    && grep -Fq 'next_refresh_at = time.time()' /app/bridge.py \
    && grep -Fq 'token_data["issued_at"]' /app/bridge.py \
    && grep -Fq '_wait_for_relogin(wait_seconds, fatal)' /app/bridge.py \
    && ! grep -Fq 'await asyncio.sleep(relogin_backoff)' /app/bridge.py \
    && grep -Fq 'async def _enforce_access_control' /app/bridge.py \
    && grep -Fq 'async def reveal_direct_token' /app/bridge.py \
    && grep -Fq 'async def xiaomi_otp_start' /app/bridge.py \
    && grep -Fq 'async def xiaomi_otp_send' /app/bridge.py \
    && grep -Fq 'async def xiaomi_otp_verify' /app/bridge.py \
    && grep -Fq 'async def xiaomi_otp_restart' /app/bridge.py \
    && grep -Fq 'async def xiaomi_cookie_login' /app/bridge.py \
    && ! grep -Fq '链接可能已过期' /app/bridge.py \
    && grep -Fq 'proxy_headers=False' /app/bridge.py \
    && ! grep -Fq 'ssl=False' /app/bridge.py \
    && ! grep -Fq 'str(TOKEN_PATH)' /app/bridge.py \
    && rm /tmp/patch_auth.py

# Build-time regression suite: login/token/credential simulation, HTTP-level
# access control, the HAOS Ingress contract and the two Xiaomi login flows
# (password + verification code, manual Cookie). They only need the runtime
# dependencies, so a broken patch fails the build instead of reaching users.
COPY test_startup_patch.py test_api_auth.py test_ingress_contract.py test_poll_auth.py test_sms_login.py test_xiaomi_login.py /tmp/
RUN python /tmp/test_startup_patch.py /app/bridge.py \
    && python /tmp/test_api_auth.py /app \
    && python /tmp/test_ingress_contract.py \
         --bridge /app/bridge.py --html /app/web/index.html --no-node \
    && python /tmp/test_poll_auth.py /app/bridge.py \
    && python /tmp/test_sms_login.py /app/bridge.py \
    && python /tmp/test_xiaomi_login.py /app/bridge.py --html /app/web/index.html \
    && rm /tmp/test_startup_patch.py /tmp/test_api_auth.py \
          /tmp/test_ingress_contract.py /tmp/test_poll_auth.py \
          /tmp/test_sms_login.py /tmp/test_xiaomi_login.py

RUN rm -rf /app/config /app/logs \
    && ln -s /data/config /app/config \
    && ln -s /data/logs /app/logs

# Keep this add-on's license separate from the upstream copyright and preserve
# the upstream README's original MIT declaration in the distributed image.
COPY LICENSE THIRD_PARTY_NOTICES.md /usr/share/licenses/xiaoai-ha-bridge-haos/
RUN mkdir -p /usr/share/licenses/xiaoai-ha-bridge \
    && cp /app/README.md /usr/share/licenses/xiaoai-ha-bridge/README.md

EXPOSE 47521

LABEL org.opencontainers.image.source="https://github.com/tarynwyj/xiaoai-ha-bridge-haos" \
      org.opencontainers.image.description="XiaoAI to Home Assistant bridge for HAOS" \
      io.hass.name="XiaoAI HA Bridge" \
      io.hass.description="XiaoAI voice bridge for Home Assistant" \
      io.hass.version="${APP_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="amd64"

CMD ["sh", "-c", "mkdir -p /data/config /data/logs && exec python -u bridge.py"]
