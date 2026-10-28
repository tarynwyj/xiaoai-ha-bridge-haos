# XiaoAI HA Bridge for Home Assistant OS

Home Assistant App wrapper for [chenshuhe/xiaoai-ha-bridge](https://github.com/chenshuhe/xiaoai-ha-bridge).

## Install

Add this repository to the Home Assistant App Store:

`https://github.com/tarynwyj/xiaoai-ha-bridge-haos`

Then install **XiaoAI HA Bridge** from that repository.

## Image

The app uses the prebuilt image:

`ghcr.io/tarynwyj/xiaoai-ha-bridge-haos:<version>`

The version in `xiaoai_bridge/config.yaml` is the single source of truth for both the App Store metadata and the image tag.

## Interface

- **Recommended:** the **XiaoAI HA Bridge** entry in the Home Assistant sidebar
  (HAOS Ingress, uses your Home Assistant admin login, no extra token).
- **Direct port:** `http://<haos-host>:47521` still works but requires the
  direct-access token in `/data/config/.access_token` (also printed in the add-on
  log); `Authorization: Bearer <token>` works for scripts.

See `xiaoai_bridge/DOCS.md` for the upgrade checklist and rollback steps.

## Local development

The Docker image pins a known upstream XiaoAI HA Bridge commit and applies the authentication and web UI patches in `xiaoai_bridge/` during build.

Build inputs are reproducible: `xiaoai_bridge/requirements.lock.txt` pins the exact runtime
dependency versions (the upstream `requirements.txt` only declares lower bounds), and the
image build runs two regression suites against the freshly patched app before shipping:

- `xiaoai_bridge/test_startup_patch.py` — login, token refresh/backoff and credential
  lifecycle simulations (stdlib only, no network);
- `xiaoai_bridge/test_api_auth.py` — HTTP-level access-control tests driving the real
  ASGI app (unauthenticated 401 on every `/api/*` route, `/api/config` masking,
  cross-site rejection, Ingress trust, credential clearing).

Run them locally against a patched tree:

```sh
python xiaoai_bridge/patch_auth.py  /tmp/bridge.py     # patched from the pinned upstream commit
python xiaoai_bridge/patch_web.py   /tmp/index.html
python xiaoai_bridge/test_startup_patch.py /tmp/bridge.py
python xiaoai_bridge/test_api_auth.py /tmp            # /tmp must contain bridge.py and web/index.html
python xiaoai_bridge/test_web_patch.py --pristine /tmp/index-pristine.html
```

CI additionally compiles every file, refuses patches that no longer match exactly once,
greps for `ssl=False` / `str(TOKEN_PATH)` regressions, builds the image, runs the same
suites inside the image, verifies the ingress metadata and the Home Assistant version
label, and only then pushes the image.
