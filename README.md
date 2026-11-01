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
  direct-access token stored in `/data/config/.access_token` (view it in the panel's
  Advanced page; the log only names the file, never the token). `Authorization: Bearer
  <token>` and `X-Admin-Token: <token>` work for scripts.

See `xiaoai_bridge/DOCS.md` for the upgrade checklist and rollback steps.

## Local development

The Docker image pins a known upstream XiaoAI HA Bridge commit and applies the authentication and web UI patches in `xiaoai_bridge/` during build.

Build inputs are reproducible: `xiaoai_bridge/requirements.lock.txt` pins the exact runtime
dependency versions (the upstream `requirements.txt` only declares lower bounds). The image
build itself refuses to finish unless the freshly patched app passes these suites:

- `xiaoai_bridge/test_startup_patch.py` — login, token refresh/backoff and credential
  lifecycle simulations (stdlib only, no network);
- `xiaoai_bridge/test_api_auth.py` — HTTP-level access control on the real ASGI app
  (unauthenticated 401 on every `/api/*` route, `/api/config` masking, cross-site
  rejection, Ingress trust, credential clearing);
- `xiaoai_bridge/test_ingress_contract.py` — HAOS Ingress contract: `ingress_port` vs the
  actually listened port, relative `fetch` rewriting under the ingress prefix, supervisor
  peer trust, and the direct-token endpoint not leaking to unauthorised callers;
- `xiaoai_bridge/test_poll_auth.py` — the conversation poll really carries the Xiaomi
  auth cookies (not just `deviceId`), keeps the selected speaker, and never sends them to
  a redirect target.

Run them locally against a patched tree:

```sh
python xiaoai_bridge/patch_auth.py  /tmp/app/bridge.py   # patched from the pinned upstream commit
python xiaoai_bridge/patch_web.py   /tmp/app/web/index.html
python xiaoai_bridge/test_startup_patch.py /tmp/app/bridge.py
python xiaoai_bridge/test_poll_auth.py /tmp/app/bridge.py
python xiaoai_bridge/test_api_auth.py /tmp/app
python xiaoai_bridge/test_web_patch.py --pristine /tmp/pinned/index.html
python xiaoai_bridge/test_ingress_contract.py --bridge /tmp/app/bridge.py \
    --html /tmp/app/web/index.html --config xiaoai_bridge/config.yaml --repo-root .
python xiaoai_bridge/test_upgrade_data.py --pristine-bridge /tmp/pinned/bridge.py \
    --pristine-html /tmp/pinned/index.html --repo . --require-legacy
```

## CI and release process

Two workflows, deliberately separate:

| Workflow | Trigger | What it does | Publishes? |
|---|---|---|---|
| `.github/workflows/ci.yml` | `pull_request`, push to `main`, manual | Compiles everything, rebuilds from the pinned upstream, runs every test suite on the host **and** builds the docker image to run the in-image tests (`load: true`, `push: false`) | **No.** No registry login, no `packages: write` scope, `push: false`, plus a guard step that fails if either ever appears |
| `.github/workflows/release.yml` | `workflow_dispatch` **only** | Re-runs the release gate, verifies `inputs.version == config.yaml`, refuses to overwrite an existing `<version>` tag, builds + smoke-tests, pushes **only** `<version>` | Only the immutable version tag; `:latest` is never pushed and asking for it fails the run |

Practical flow:

1. Open a pull request. The free CI builds the image and runs every test — nothing is published.
2. Merge (or push) to `main`: the same test pipeline runs again. **No image is published.**
3. When you decide to publish: Actions → **Release image** → *Run workflow* → branch `main`,
   `version` = the value in `xiaoai_bridge/config.yaml`, keep `overwrite_existing=false`,
   keep `allow_latest=false`.
4. HAOS resolves the image tag from `config.yaml` (`image:` + `version`), so an add-on install
   never depends on `:latest`; not moving `latest` cannot break existing installs.

Recommended repository setting: create an environment named `release` with required
reviewers, so a manual release needs an approval click on top of the manual dispatch.
