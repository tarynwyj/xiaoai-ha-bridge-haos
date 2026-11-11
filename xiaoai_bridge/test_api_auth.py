"""Real HTTP-level tests for the bridge web API access control.

Loads the *patched* ``bridge.py`` as a module inside a throw-away /data tree and
drives the ASGI application directly (no network, no httpx, no starlette
TestClient), so the suite runs inside the built image with runtime deps only.

All credentials below are synthetic.

Usage: python test_api_auth.py [/app]
"""

import asyncio
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

app_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "/app").resolve()
bridge_source = app_dir / "bridge.py"
assert bridge_source.exists(), f"patched bridge.py not found at {bridge_source}"

FAKE_COOKIE = (
    "userId=test-user; passToken=test-pass; serviceToken=test-service; deviceId=test-device"
)
FAKE_PASSWORD = "test-password"
FAKE_HA_TOKEN = "test-ha-token"
FAKE_OPENAI_KEY = "sk-test-key"
FAKE_USER = "test-user"
# Every non-GET call made by the patched UI carries this header (see patch_web.py);
# cookie-authenticated state changes require it as a second CSRF barrier.
UI_HEADER = {"X-Requested-With": "xiaoai-bridge"}

CONFIG = {
    "xiaomi": {
        "username": FAKE_USER,
        "password": FAKE_PASSWORD,
        "cookie_text": FAKE_COOKIE,
        "device_did": "speaker-1",
        "hardware": "L05C",
        "tts_reply": True,
    },
    "homeassistant": {"url": "http://homeassistant:80", "token": FAKE_HA_TOKEN},
    "openai": {
        "api_base": "https://api.example.invalid",
        "api_key": FAKE_OPENAI_KEY,
        "model": "test-model",
    },
    "ai_mode": False,
    "poll_interval_seconds": 2,
    "intent_rules": [],
}


# ───────────────────────── minimal ASGI test client ─────────────────────────
class Response:
    def __init__(self, status_code, headers, body):
        self.status_code = status_code
        self.headers = headers
        self.body = body

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body)

    def header(self, name):
        name = name.lower()
        for key, value in self.headers:
            if key.decode("latin-1").lower() == name:
                return value.decode("latin-1")
        return None


def call(app, method, path, *, headers=None, json_body=None, body=b"",
         cookies=None, client=("testclient", 50000)):
    """Invoke the ASGI app once and collect the response."""
    header_pairs = []
    payload = body
    if json_body is not None:
        payload = json.dumps(json_body).encode("utf-8")
        header_pairs.append((b"content-type", b"application/json"))
    if payload:
        header_pairs.append((b"content-length", str(len(payload)).encode()))
    for key, value in (headers or {}).items():
        header_pairs.append((key.lower().encode("latin-1"), value.encode("latin-1")))
    if not any(name == b"host" for name, _ in header_pairs):
        # Real browsers always send Host; the CSRF check compares Origin against it.
        header_pairs.append((b"host", b"testserver"))
    if cookies:
        joined = "; ".join(f"{key}={value}" for key, value in cookies.items())
        header_pairs.append((b"cookie", joined.encode("latin-1")))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "root_path": "",
        "headers": header_pairs,
        "client": client,
        "server": ("testserver", 80),
        "state": {},
    }
    sent = {"done": False}
    received = {"status": 0, "headers": [], "body": bytearray()}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            received["status"] = message["status"]
            received["headers"] = list(message.get("headers") or [])
        elif message["type"] == "http.response.body":
            received["body"].extend(message.get("body", b""))

    asyncio.run(app(scope, receive, send))
    return Response(received["status"], received["headers"], bytes(received["body"]))


class Client:
    """Tracks cookies across calls, like a browser session."""

    def __init__(self, app):
        self.app = app
        self.cookies = {}

    def request(self, method, path, **kwargs):
        kwargs.setdefault("cookies", dict(self.cookies) or None)
        response = call(self.app, method, path, **kwargs)
        set_cookie = response.header("set-cookie")
        if set_cookie:
            pair = set_cookie.split(";", 1)[0].strip()
            if "=" in pair:
                name, value = pair.split("=", 1)
                if value:
                    self.cookies[name] = value
                else:
                    self.cookies.pop(name, None)
        return response

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def delete(self, path, **kwargs):
        return self.request("DELETE", path, **kwargs)


# ───────────────────────── harness ─────────────────────────
def read_config(root: Path) -> dict:
    return yaml.safe_load((root / "config" / "config.yaml").read_text(encoding="utf-8"))


def build_runtime(root: Path) -> Path:
    """Create a /data-like tree with the patched app and a config holding secrets."""
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "web").mkdir(parents=True, exist_ok=True)
    shutil.copy(bridge_source, root / "bridge.py")
    shutil.copy(app_dir / "web" / "index.html", root / "web" / "index.html")
    (root / "config" / "config.yaml").write_text(
        yaml.safe_dump(CONFIG, allow_unicode=True), encoding="utf-8"
    )
    # Saved login files must exist so the clear-login endpoint has something to remove.
    (root / "config" / ".mi.token").write_text(
        json.dumps(
            {
                "userId": FAKE_USER,
                "passToken": "test-pass",
                "deviceId": "test-device",
                "micoapi": ["test-ssecurity", "test-service"],
            }
        ),
        encoding="utf-8",
    )
    (root / "config" / "auth.json").write_text(
        json.dumps(
            {"userId": FAKE_USER, "passToken": "test-pass", "deviceId": "test-device"}
        ),
        encoding="utf-8",
    )
    return root


def load_bridge(root: Path):
    """Import the patched bridge.py with ``root`` as the process working directory."""
    previous = os.getcwd()
    os.chdir(root)
    try:
        spec = importlib.util.spec_from_file_location("patched_bridge_api", root / "bridge.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["patched_bridge_api"] = module
        spec.loader.exec_module(module)
    finally:
        os.chdir(previous)
    return module


def api_paths(module):
    paths = {}
    for route in module.app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None) or set()
        if path.startswith("/api/"):
            paths[path] = sorted(methods)
    return paths


def main():
    results = []

    def ok(message):
        results.append(message)

    # A plain mkdtemp + best-effort cleanup: the logging FileHandler keeps a handle
    # open on Windows, so TemporaryDirectory() cleanup would raise and hide results.
    temp_dir = Path(tempfile.mkdtemp(prefix="xiaoai-api-test-"))
    previous_cwd = os.getcwd()
    try:
        root = build_runtime(temp_dir)
        # The bridge resolves config/, logs/ and web/ relative to its working
        # directory (in the container that is /app), so stay inside the temp tree.
        os.chdir(root)
        bridge = load_bridge(root)
        token = (root / "config" / ".access_token").read_text(encoding="utf-8").strip()
        assert len(token) >= 32, "access token must be long and random"
        client = Client(bridge.app)

        # ── A: every /api route except the login endpoint requires authentication ──
        paths = api_paths(bridge)
        assert paths, "no /api routes found"
        exempt = {"/api/session"}
        for path in sorted(paths):
            if path in exempt:
                continue
            probe = path.replace("{automation_id}", "xiaoai_bridge_probe")
            for method in paths[path]:
                if method in ("GET", "HEAD"):
                    response = client.get(probe)
                elif method == "DELETE":
                    response = client.delete(probe)
                else:
                    response = client.post(probe, json_body={})
                assert response.status_code == 401, (
                    f"{method} {path} must be 401 without credentials, "
                    f"got {response.status_code}"
                )
                assert response.json().get("auth_required") is True
                assert response.header("x-bridge-auth") == "panel", (
                    f"{method} {path} must mark a panel-auth 401, "
                    f"got {response.header('x-bridge-auth')!r}"
                )
        ok(f"all {len(paths)} /api routes reject unauthenticated access")

        # ── public docs must be disabled (no schema disclosure) ──
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/docs").status_code == 404
        ok("OpenAPI schema and /docs are disabled")

        # ── the root page shows the login form, not the management UI ──
        root_page = client.get("/")
        assert root_page.status_code == 200
        assert "XiaoAI HA Bridge" in root_page.text
        assert 'type="password"' in root_page.text, "the login page needs a token input"
        assert "api/session" in root_page.text, "the login page must post to api/session"
        assert "「高级设置」页" in root_page.text, "token help must name the real settings page"
        assert "小米登录 &rarr; 高级选项" not in root_page.text
        assert "令牌只在本次运行内有效" in root_page.text
        assert 'id="page-rules"' not in root_page.text, "login page must not leak the app UI"
        ok("unauthenticated GET / serves a login page only")

        # ── login flow ──
        assert client.post("/api/session", json_body={"token": "wrong-token"}).status_code == 401
        assert client.post("/api/session", json_body={"token": ""}).status_code == 401
        # A non-ASCII token is a plain mismatch, never a server error.
        non_ascii = client.post("/api/session", json_body={"token": "口令口令口令"})
        assert non_ascii.status_code == 401, non_ascii.status_code
        latin1_header = client.get(
            "/api/status", headers={"Authorization": "Bearer tökén"}
        )
        assert latin1_header.status_code == 401, latin1_header.status_code
        login = client.post("/api/session", json_body={"token": token})
        assert login.status_code == 200 and login.json()["ok"] is True
        assert client.cookies.get(bridge.SESSION_COOKIE), "session cookie was not set"
        set_cookie = login.header("set-cookie")
        assert "httponly" in set_cookie.lower(), "session cookie must be HttpOnly"
        assert "samesite=strict" in set_cookie.lower(), "session cookie must be SameSite=Strict"
        ok("direct login rejects wrong tokens and accepts the real one")

        # ── A: /api/config must not leak secrets ──
        response = client.get("/api/config")
        assert response.status_code == 200
        body = response.json()
        masked = bridge.MASKED_VALUE
        assert body["xiaomi"]["password"] == masked
        assert body["xiaomi"]["cookie_text"] == masked, "cookie_text must be masked"
        assert body["homeassistant"]["token"] == masked
        assert body["openai"]["api_key"] == masked
        serialised = json.dumps(body, ensure_ascii=False)
        # xiaomi.username stays visible on purpose: the config page edits it in a
        # text input and would otherwise save the mask back as the account name.
        for secret in ("test-pass", "test-service", FAKE_PASSWORD, FAKE_HA_TOKEN,
                       FAKE_OPENAI_KEY):
            assert secret not in serialised, f"/api/config leaked {secret!r}"
        ok("/api/config masks every credential and leaks no secret substring")

        # ── CSRF: state-changing request from a foreign Origin is refused ──
        payload = {
            "config": {
                "xiaomi": {"username": FAKE_USER, "password": masked,
                           "cookie_text": masked},
                "homeassistant": {"url": "http://homeassistant:80", "token": masked},
                "openai": {"api_key": masked},
            }
        }
        cross = client.post(
            "/api/config",
            json_body=payload,
            headers={"Origin": "https://evil.example", **UI_HEADER},
        )
        assert cross.status_code == 403, cross.text
        # Cookie mode needs the UI header as well, so a plain form post cannot change state.
        no_header = client.post(
            "/api/config", json_body=payload, headers={"Origin": "http://testserver"}
        )
        assert no_header.status_code == 403, (
            f"cookie-authenticated POST without X-Requested-With must be 403, "
            f"got {no_header.status_code}"
        )
        same = client.post(
            "/api/config",
            json_body=payload,
            headers={"Origin": "http://testserver", **UI_HEADER},
        )
        assert same.status_code == 200, same.text
        saved = read_config(root)
        assert saved["xiaomi"]["cookie_text"] == FAKE_COOKIE, "masked save must keep the cookie"
        assert saved["homeassistant"]["token"] == FAKE_HA_TOKEN
        ok("cross-site POST is rejected (403) while same-origin save keeps secrets")

        # ── scoped config saves must preserve changes from another tab ──
        alias_saved = client.post(
            "/api/aliases", headers=UI_HEADER,
            json_body={"entity_id": "light.a", "alias": "客厅主灯"},
        )
        assert alias_saved.status_code == 200 and alias_saved.json()["ok"] is True
        scoped = client.post(
            "/api/config", headers=UI_HEADER,
            json_body={"section": "rules",
                       "revision": client.get("/api/config").json()["_section_revisions"]["rules"],
                       "config": {
                "intent_rules": [{"pattern": "开灯", "action": {
                    "domain": "light", "service": "turn_on",
                    "entity_id": "light.a", "reply": "好的"}}],
                "device_aliases": {"light.a": "过期旧别名"},
                "xiaomi": {"username": FAKE_USER, "cookie_text": ""},
            }},
        )
        assert scoped.status_code == 200 and scoped.json()["ok"] is True, scoped.text
        cfg_scoped = read_config(root)
        assert cfg_scoped["device_aliases"]["light.a"] == "客厅主灯", (
            "saving rules must not roll back a more recently saved device alias"
        )
        assert cfg_scoped["xiaomi"]["cookie_text"] == FAKE_COOKIE
        assert cfg_scoped["intent_rules"][0]["pattern"] == "开灯"
        ok("scoped rules save preserves device aliases and Xiaomi credentials")

        # A scoped Xiaomi edit must not drop hidden legacy settings that the
        # modern UI does not render; HA edits must likewise retain extra keys.
        cfg_before = read_config(root)
        cfg_before["xiaomi"]["device_id"] = "legacy-speaker-id"
        cfg_before["homeassistant"]["verify_ssl"] = True
        bridge.save_config(cfg_before)
        scoped_xiaomi = client.post(
            "/api/config", headers=UI_HEADER,
            json_body={"section": "xiaomi",
                       "revision": client.get("/api/config").json()["_section_revisions"]["xiaomi"],
                       "config": {
                "xiaomi": {"username": FAKE_USER, "password": bridge.MASKED_VALUE,
                           "cookie_text": bridge.MASKED_VALUE,
                           "hardware": "NEW-HARDWARE"}}},
        )
        assert scoped_xiaomi.status_code == 200 and scoped_xiaomi.json()["ok"] is True
        saved_xiaomi = read_config(root)
        assert saved_xiaomi["xiaomi"]["device_id"] == "legacy-speaker-id"
        assert saved_xiaomi["xiaomi"]["hardware"] == "NEW-HARDWARE"
        assert saved_xiaomi["homeassistant"]["verify_ssl"] is True
        scoped_ha = client.post(
            "/api/config", headers=UI_HEADER,
            json_body={"section": "ha",
                       "revision": client.get("/api/config").json()["_section_revisions"]["ha"],
                       "config": {
                "homeassistant": {"url": "http://homeassistant:80",
                                  "token": bridge.MASKED_VALUE}}},
        )
        assert scoped_ha.status_code == 200 and scoped_ha.json()["ok"] is True
        assert read_config(root)["homeassistant"]["verify_ssl"] is True
        assert read_config(root)["homeassistant"]["token"] == FAKE_HA_TOKEN
        if os.name == "posix":
            assert os.stat(root / "config" / "config.yaml").st_mode & 0o777 == 0o600
        ok("scoped Xiaomi/HA saves retain hidden settings and config stays mode 0600")

        # ── listening scope: save -> disk -> reload -> poll targets ──
        cookie_before = read_config(root)["xiaomi"].get("cookie_text", "")
        scope_rev = client.get("/api/config").json()["_section_revisions"]["xiaomi"]
        scope_save = client.post(
            "/api/config", headers=UI_HEADER,
            json_body={"section": "xiaomi", "revision": scope_rev,
                       "config": {
                "xiaomi": {"username": FAKE_USER, "password": bridge.MASKED_VALUE,
                           "cookie_text": bridge.MASKED_VALUE,
                           "hardware": "L05C",
                           "device_scope": "selected",
                           "device_ids": ["speaker-A", "speaker-C"],
                           "device_id": "", "device_did": ""}}},
        )
        assert scope_save.status_code == 200 and scope_save.json()["ok"] is True, \
            scope_save.text
        on_disk = read_config(root)["xiaomi"]
        assert on_disk["device_scope"] == "selected", on_disk
        assert on_disk["device_ids"] == ["speaker-A", "speaker-C"], on_disk
        assert on_disk["username"] == FAKE_USER, on_disk
        assert on_disk.get("cookie_text", "") == cookie_before, (
            "saving the listening scope must not rewrite the stored cookie"
        )
        reloaded = client.get("/api/config").json()
        assert reloaded["xiaomi"]["device_ids"] == ["speaker-A", "speaker-C"], \
            "the saved scope must read back through the API"
        devices = [
            {"deviceID": "speaker-A", "hardware": "L05C"},
            {"deviceID": "speaker-B", "hardware": "L05C"},
            {"deviceID": "speaker-C", "hardware": "L06A"},
        ]
        targets = bridge._poll_targets(on_disk, devices)
        assert [t["deviceID"] for t in targets] == ["speaker-A", "speaker-C"], targets
        ok("a saved listening scope survives the round trip and drives _poll_targets")

        before_scope = (root / "config" / "config.yaml").read_bytes()
        for invalid in ([], [" "], [None], 42):
            refused = client.post("/api/config", headers=UI_HEADER, json_body={
                "section": "xiaomi",
                "revision": client.get("/api/config").json()["_section_revisions"]["xiaomi"],
                "config": {"xiaomi": {"device_scope": "selected", "device_ids": invalid,
                                       "device_id": "", "device_did": ""}}})
            assert refused.status_code == 400, refused.text
            assert (root / "config" / "config.yaml").read_bytes() == before_scope
        legacy_refused = client.post("/api/config", headers=UI_HEADER, json_body={
            "config": {"xiaomi": {"device_scope": "selected", "device_ids": []}}})
        assert legacy_refused.status_code == 400, legacy_refused.text
        assert bridge._poll_targets({"device_scope": "selected", "device_ids": []}, devices) == []
        ok("empty/invalid selections are refused by scoped and legacy APIs and fail closed at runtime")

        all_rev = client.get("/api/config").json()["_section_revisions"]["xiaomi"]
        all_save = client.post(
            "/api/config", headers=UI_HEADER,
            json_body={"section": "xiaomi", "revision": all_rev,
                       "config": {
                "xiaomi": {"username": FAKE_USER, "password": bridge.MASKED_VALUE,
                           "cookie_text": bridge.MASKED_VALUE,
                           "device_scope": "all", "device_ids": [],
                           "device_id": "", "device_did": ""}}},
        )
        assert all_save.status_code == 200 and all_save.json()["ok"] is True, all_save.text
        back_to_all = read_config(root)["xiaomi"]
        assert back_to_all["device_scope"] == "all", back_to_all
        assert back_to_all["device_ids"] == [], back_to_all
        assert [t["deviceID"] for t in bridge._poll_targets(back_to_all, devices)] == \
            ["speaker-A", "speaker-B", "speaker-C"], "all mode must poll every device"
        stale_rev = client.get("/api/config").json()["_section_revisions"]["xiaomi"]
        conflict = client.post(
            "/api/config", headers=UI_HEADER,
            json_body={"section": "xiaomi", "revision": "v1:stale",
                       "config": {"xiaomi": {"device_scope": "selected",
                                             "device_ids": ["speaker-B"]}}},
        )
        assert conflict.status_code == 409, (
            "a stale revision must be rejected, not silently overwrite the scope"
        )
        assert read_config(root)["xiaomi"]["device_scope"] == "all", (
            "a rejected save must leave the stored scope untouched"
        )
        assert stale_rev != "v1:stale"
        ok("switching back to all clears the allow list and a stale save is rejected")

        # ── real CAS semantics: same section conflicts, other sections do not ──
        page_a = client.get("/api/config").json()["_section_revisions"].copy()
        page_b = client.get("/api/config").json()["_section_revisions"].copy()
        assert all(str(v).startswith("v1:") for v in page_a.values())
        first = client.post(
            "/api/config", headers=UI_HEADER,
            json_body={"section": "ha", "revision": page_a["ha"],
                       "config": {"homeassistant": {
                           "url": "http://homeassistant:80",
                           "token": masked, "cas_note": "tab-a"}}},
        )
        assert first.status_code == 200 and first.json()["ok"], first.text
        assert first.json()["revision"] != page_a["ha"]
        previous = (root / "config" / "config.yaml").read_bytes()
        stale = client.post(
            "/api/config", headers=UI_HEADER,
            json_body={"section": "ha", "revision": page_b["ha"],
                       "config": {"homeassistant": {
                           "url": "http://homeassistant:80",
                           "token": masked, "cas_note": "tab-b"}}},
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["code"] == "config_conflict"
        assert (root / "config" / "config.yaml").read_bytes() == previous
        assert read_config(root)["homeassistant"]["cas_note"] == "tab-a"
        ok("stale second tab gets 409 and cannot overwrite saved HA changes")

        # The same stale page's rules revision is still current: independent tabs.
        independent = client.post(
            "/api/config", headers=UI_HEADER,
            json_body={"section": "rules", "revision": page_b["rules"],
                       "config": {"intent_rules": cfg_scoped["intent_rules"] + [{
                           "pattern": "关灯", "action": {
                               "domain": "light", "service": "turn_off",
                               "entity_id": "light.a"}}]}},
        )
        assert independent.status_code == 200 and independent.json()["ok"], independent.text
        second = client.post(
            "/api/config", headers=UI_HEADER,
            json_body={"section": "ha", "revision": first.json()["revision"],
                       "config": {"homeassistant": {
                           "url": "http://homeassistant:80",
                           "token": masked, "cas_note": "tab-a-second"}}},
        )
        assert second.status_code == 200 and second.json()["ok"], second.text
        assert second.json()["revision"] != first.json()["revision"]
        assert read_config(root)["homeassistant"]["cas_note"] == "tab-a-second"
        ok("different sections coexist and fresh CAS tokens allow consecutive saves")

        old_rules = read_config(root)["intent_rules"]
        absent = client.post(
            "/api/config", headers=UI_HEADER,
            json_body={"section": "rules", "config": {"intent_rules": []}},
        )
        assert absent.status_code == 428 and absent.json()["code"] == "revision_required"
        assert read_config(root)["intent_rules"] == old_rules
        stale_rules = client.post(
            "/api/config", headers=UI_HEADER,
            json_body={"section": "rules", "revision": page_a["rules"],
                       "config": {"intent_rules": []}},
        )
        assert stale_rules.status_code == 409
        assert read_config(root)["intent_rules"] == old_rules
        ok("CAS is required even for deliberate clear-all and rejects stale rule deletion")


        # ── test rules default to pure preview; never trigger HA services ──
        preview = client.post(
            "/api/test/rule", headers=UI_HEADER,
            json_body={"text": "开灯", "rules": cfg_scoped["intent_rules"]},
        )
        assert preview.status_code == 200 and preview.json()["matched"] is True, preview.text
        assert preview.json()["preview"] is True
        assert preview.json()["executed"] is False
        assert preview.json()["action"]["entity_id"] == "light.a"
        ok("rule test is preview-only unless execution is explicitly requested")

        # ── forged ingress header from a non-supervisor peer must not authenticate ──
        client.cookies.clear()
        forged = client.get(
            "/api/status", headers={"X-Ingress-Path": "/api/hassio_ingress/x"}
        )
        assert forged.status_code == 401, "X-Ingress-Path must not be trusted from any peer"
        assert bridge._peer_is_supervisor("172.30.32.2") is True
        assert bridge._peer_is_supervisor("172.30.33.7") is False, (
            "other containers in 172.30.32.0/23 must not be trusted"
        )
        assert bridge._peer_is_supervisor("192.168.1.10") is False
        assert bridge._peer_is_supervisor("testclient") is False
        # Same header, but claiming to come from the supervisor's address.
        supervisor_view = client.get(
            "/api/status",
            headers={"X-Ingress-Path": "/api/hassio_ingress/x"},
            client=("172.30.32.2", 41234),
        )
        assert supervisor_view.status_code == 200, supervisor_view.text
        ok("X-Ingress-Path is only trusted from the supervisor peer address")

        # ── ingress requests stay masked ──
        ingress_config = client.get(
            "/api/config",
            headers={"X-Ingress-Path": "/api/hassio_ingress/x"},
            client=("172.30.32.2", 41234),
        )
        assert ingress_config.json()["xiaomi"]["cookie_text"] == masked
        # A Home Assistant admin must never be locked out of the panel: the ingress
        # root has to serve the real UI, not the direct-access login page.
        ingress_root = client.get(
            "/",
            headers={"X-Ingress-Path": "/api/hassio_ingress/x"},
            client=("172.30.32.2", 41234),
        )
        assert ingress_root.status_code == 200
        assert 'id="page-rules"' in ingress_root.text, (
            "ingress must serve the management UI, not the login page"
        )
        ok("ingress requests from the supervisor are authorised and still masked")

        # ── the direct token is only revealed inside the HA panel, never in logs ──
        assert client.get("/api/auth/token").status_code == 401, "must require auth first"
        client.post("/api/session", json_body={"token": token})
        guarded = client.get("/api/auth/token")
        assert guarded.status_code == 403, (
            f"a direct session must not reveal the token, got {guarded.status_code}"
        )
        revealed = client.get(
            "/api/auth/token",
            headers={"X-Ingress-Path": "/api/hassio_ingress/x"},
            client=("172.30.32.2", 41234),
        )
        assert revealed.status_code == 200, revealed.text
        assert revealed.json()["token"] == token
        ok("direct token is retrievable only through the ingress panel")

        # ── bearer token works for scripted access ──
        client.cookies.clear()
        bearer = client.get("/api/status", headers={"Authorization": f"Bearer {token}"})
        assert bearer.status_code == 200
        admin_header = client.get("/api/status", headers={"X-Admin-Token": token})
        assert admin_header.status_code == 200
        assert client.get(
            "/api/status", headers={"Authorization": "Bearer wrong-token"}
        ).status_code == 401
        ok("Bearer and X-Admin-Token authenticate direct API calls")

        # ── logout ──
        client.post("/api/session", json_body={"token": token})
        old_cookie = client.cookies.get(bridge.SESSION_COOKIE)
        assert old_cookie
        assert client.post("/api/logout", headers=UI_HEADER).status_code == 200
        assert client.get("/api/status", cookies={bridge.SESSION_COOKIE: old_cookie}).status_code == 401, (
            "a replay of a logged-out cookie must be refused by the server"
        )
        assert client.get("/api/status").status_code == 401
        fresh_login = client.post("/api/session", json_body={"token": token})
        assert fresh_login.status_code == 200
        fresh_cookie = client.cookies.get(bridge.SESSION_COOKIE)
        assert fresh_cookie != old_cookie
        assert client.get("/api/status", cookies={bridge.SESSION_COOKIE: fresh_cookie}).status_code == 200
        bridge._active_sessions[bridge._session_key(fresh_cookie)] = 1
        assert client.get("/api/status", cookies={bridge.SESSION_COOKIE: fresh_cookie}).status_code == 401, (
            "an expired cookie must be invalid even if the browser still sends it"
        )
        client.cookies.clear()
        ok("logout revokes replayed direct cookies, new sessions are unique, expiry is enforced")

        # ── E: explicit credential clear removes files and stored cookie ──
        client.post("/api/session", json_body={"token": token})

        # ── clear_login must compare CAS BEFORE any irreversible operation ──
        auth_file = root / "config" / "auth.json"
        token_file = root / "config" / ".mi.token"
        auth_file.write_text('{"userId":"cas-user","passToken":"cas-pass","deviceId":"cas-device"}', encoding="utf-8")
        token_file.write_text('{"userId":"cas-user","passToken":"cas-pass","deviceId":"cas-device","micoapi":["sec","svc"]}', encoding="utf-8")
        config_before_clear = read_config(root)
        config_before_clear["xiaomi"]["password"] = "cas-secret"
        config_before_clear["xiaomi"]["cookie_text"] = "userId=cas-user; passToken=cas-pass"
        bridge.save_config(config_before_clear)
        previous_rev = client.get("/api/config").json()["_section_revisions"]["xiaomi"]

        # Another tab modifies the same Xiaomi section.
        changed = read_config(root)
        changed["xiaomi"]["hardware"] = "new-hardware"
        bridge.save_config(changed)
        old_data = (root / "config" / "config.yaml").read_bytes()
        old_auth = auth_file.read_bytes()
        old_token = token_file.read_bytes()
        stale_clear = client.post(
            "/api/config", headers=UI_HEADER,
            json_body={"section": "xiaomi", "revision": previous_rev,
                       "config": {"xiaomi": {"clear_login": True}}},
        )
        assert stale_clear.status_code == 409 and stale_clear.json()["code"] == "config_conflict"
        assert (root / "config" / "config.yaml").read_bytes() == old_data
        assert auth_file.read_bytes() == old_auth and token_file.read_bytes() == old_token
        ok("stale CAS clear_login returns 409 without deleting credentials or changing config")

        current_rev = client.get("/api/config").json()["_section_revisions"]["xiaomi"]
        fresh_clear = client.post(
            "/api/config", headers=UI_HEADER,
            json_body={"section": "xiaomi", "revision": current_rev,
                       "config": {"xiaomi": {"clear_login": True}}},
        )
        assert fresh_clear.status_code == 200 and fresh_clear.json()["ok"] is True, fresh_clear.text
        assert fresh_clear.json()["revision"] == client.get("/api/config").json()["_section_revisions"]["xiaomi"]
        assert not auth_file.exists() and not token_file.exists()
        assert read_config(root)["xiaomi"]["password"] == ""
        assert read_config(root)["xiaomi"]["cookie_text"] == ""
        ok("fresh CAS clear_login succeeds and returns the new Xiaomi revision")

        # Incomplete legacy config writes must preserve unrelated settings.
        before_legacy = read_config(root)
        legacy_partial = client.post(
            "/api/config", headers=UI_HEADER,
            json_body={"config": {"xiaomi": {"hardware": "legacy-only-change"}}},
        )
        assert legacy_partial.status_code == 200 and legacy_partial.json()["ok"], legacy_partial.text
        after_legacy = read_config(root)
        assert after_legacy["xiaomi"]["hardware"] == "legacy-only-change"
        assert after_legacy["homeassistant"] == before_legacy["homeassistant"]
        assert after_legacy["intent_rules"] == before_legacy["intent_rules"]
        assert after_legacy["device_aliases"] == before_legacy["device_aliases"]
        ok("legacy partial save does not erase HA, rules, aliases or other Xiaomi keys")

        auth_file.write_bytes(old_auth)
        token_file.write_bytes(old_token)
        restored = read_config(root)
        restored["xiaomi"]["password"] = "secret-to-clear"
        restored["xiaomi"]["cookie_text"] = "userId=cas-user; passToken=cas-pass"
        bridge.save_config(restored)
        legacy_clear = client.post(
            "/api/config", headers=UI_HEADER,
            json_body={"config": {"xiaomi": {"clear_login": True}}},
        )
        assert legacy_clear.status_code == 200 and legacy_clear.json()["ok"], legacy_clear.text
        assert not auth_file.exists() and not token_file.exists()
        assert read_config(root)["xiaomi"]["password"] == ""
        assert read_config(root)["xiaomi"]["cookie_text"] == ""
        assert read_config(root)["intent_rules"] == before_legacy["intent_rules"]
        assert read_config(root)["homeassistant"] == before_legacy["homeassistant"]
        ok("legacy partial clear_login clears only login and preserves all unrelated config")

        class FakePendingLogin:
            closed = False

            async def close(self):
                self.closed = True

        otp_pending = FakePendingLogin()
        legacy_pending = FakePendingLogin()
        bridge._otp_session = otp_pending
        bridge._otp_login.update({"username": "test", "created_at": 1})
        bridge._pending_session = legacy_pending
        bridge._pending_login.update({"username": "test"})
        bridge._qr_state.update({"ready": True, "lp_url": "https://example.invalid"})
        cleared = client.post("/api/xiaomi/clear-login", headers=UI_HEADER)
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["ok"] is True
        assert otp_pending.closed and legacy_pending.closed
        assert not bridge._otp_login and not bridge._pending_login and not bridge._qr_state

        assert not (root / "config" / ".mi.token").exists()
        assert not (root / "config" / "auth.json").exists()
        cfg_after = read_config(root)
        assert cfg_after["xiaomi"]["cookie_text"] == ""
        assert cfg_after["xiaomi"]["password"] == ""
        # A later save must not resurrect the wiped credentials.
        client.post("/api/config", json_body={"config": cfg_after}, headers=UI_HEADER)
        cfg_final = read_config(root)
        assert cfg_final["xiaomi"]["cookie_text"] == ""
        assert not (root / "config" / ".mi.token").exists()
        ok("cleared credentials stay cleared (no silent resurrection)")

        # ── 刷新音箱列表 must ask Xiaomi, not replay the /api/status cache ──
        import types as _types

        token_file.write_text(json.dumps({
            "userId": "speaker-user", "passToken": "speaker-pass",
            "deviceId": "login-device", "micoapi": ["speaker-sec", "speaker-service"],
        }), encoding="utf-8")
        auth_file.write_text(json.dumps({
            "userId": "speaker-user", "passToken": "speaker-pass",
            "deviceId": "login-device",
        }), encoding="utf-8")
        speaker_calls = []

        class FakeMiAccount:
            def __init__(self, *args, **kwargs):
                self.token = {}

        class FakeMiNAService:
            def __init__(self, account):
                self.account = account

            async def device_list(self):
                speaker_calls.append(self.account)
                return [{"deviceID": "speaker-live", "name": "客厅音箱",
                         "hardware": "L05C", "miotDID": "internal-only"}]

        fake_miservice = _types.ModuleType("miservice")
        fake_miservice.MiAccount = FakeMiAccount
        fake_miservice.MiNAService = FakeMiNAService
        fake_miaccount = _types.ModuleType("miservice.miaccount")
        fake_miaccount.get_random = lambda n: "LOGINDEVICEID"
        previous_modules = {name: sys.modules.get(name)
                            for name in ("miservice", "miservice.miaccount")}
        sys.modules["miservice"] = fake_miservice
        sys.modules["miservice.miaccount"] = fake_miaccount
        try:
            bridge.bridge_state["devices"] = [
                {"deviceID": "stale-cache", "name": "旧缓存音箱"}]
            live = client.get("/api/xiaomi/speakers",
                              headers={"Authorization": f"Bearer {token}"})
            assert live.status_code == 200, live.text
            payload = live.json()
            assert payload["ok"] is True and payload["source"] == "xiaomi"
            assert [d["deviceID"] for d in payload["devices"]] == ["speaker-live"]
            assert "miotDID" not in payload["devices"][0]
            assert len(speaker_calls) == 1, "device_list must be called for 刷新"
            assert bridge.bridge_state["devices"][0]["deviceID"] == "speaker-live"
            ok("刷新音箱列表 calls Xiaomi device_list and ignores the status cache")

            bridge.bridge_state["devices"] = [
                {"deviceID": "cached-speaker", "name": "缓存音箱"}]

            class FailingMiNAService(FakeMiNAService):
                async def device_list(self):
                    raise TimeoutError("scripted Xiaomi timeout")

            fake_miservice.MiNAService = FailingMiNAService
            failed = client.get("/api/xiaomi/speakers",
                                headers={"Authorization": f"Bearer {token}"})
            assert failed.status_code == 502, failed.text
            failed_payload = failed.json()
            assert failed_payload["ok"] is False
            assert [d["deviceID"] for d in failed_payload["devices"]] == ["cached-speaker"], (
                "a failed refresh must return the cached list instead of clearing it"
            )
            ok("failed 刷新 returns 502 with the cached list, never an empty one")

            token_file.unlink()
            auth_file.unlink()
            bridge.bridge_state["devices"] = []
            anonymous = client.get("/api/xiaomi/speakers",
                                   headers={"Authorization": f"Bearer {token}"})
            assert anonymous.status_code == 401, anonymous.text
            assert anonymous.header("x-bridge-auth") == "xiaomi", (
                "a Xiaomi-login 401 must not look like an expired panel session"
            )
            assert anonymous.json()["code"] == "login_required"
            ok("刷新 without saved Xiaomi credentials answers 401 login_required")
        finally:
            for name, module in previous_modules.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

        # ── deterministically race a stale authentication request with clear ──
        async def race_stale_write_against_clear():
            begun = asyncio.Event()
            resume = asyncio.Event()
            identity = {"userId": "stale", "passToken": "stale-pass",
                        "deviceId": "stale-dev", "ssecurity": "stale-sec",
                        "serviceToken": "stale-service"}

            async def delayed_authenticated_request():
                # Middleware captures this generation on request entry.
                bridge._credential_context.set(bridge._credential_generation)
                begun.set()
                await resume.wait()
                try:
                    bridge._persist_sms_credentials(identity)
                except RuntimeError as exc:
                    return "旧请求" in str(exc)
                return False

            pending = asyncio.create_task(delayed_authenticated_request())
            await begun.wait()
            await bridge.clear_xiaomi_login()
            resume.set()
            return await pending

        assert asyncio.run(race_stale_write_against_clear()), (
            "an in-flight old login must be refused after credential clear"
        )
        assert not (root / "config" / ".mi.token").exists()
        assert not (root / "config" / "auth.json").exists()
        assert read_config(root)["xiaomi"]["cookie_text"] == ""
        ok("in-flight authentication cannot resurrect credentials after clear")

        missing_rules = client.post(
            "/api/config", headers=UI_HEADER,
            json_body={"section": "rules",
                           "revision": client.get("/api/config").json()["_section_revisions"]["rules"],
                           "config": {}},
        )
        assert missing_rules.status_code == 422 and missing_rules.json()["ok"] is False
        assert read_config(root)["intent_rules"][0]["pattern"] == "开灯"
        ok("omitting intent_rules never erases saved rules")


        # ── middleware owns credential context generation, not just a test stub ──
        from fastapi import Request

        @bridge.app.get("/api/__generation_probe")
        async def generation_probe(request: Request):
            return {
                "captured": bridge._credential_context.get(),
                "current": bridge._credential_generation,
                "allowed": bridge._credential_write_allowed(),
            }

        first_epoch = client.get(
            "/api/__generation_probe",
            headers={"Authorization": "Bearer " + token},
        )
        assert first_epoch.status_code == 200, first_epoch.text
        assert first_epoch.json()["captured"] == first_epoch.json()["current"]
        assert first_epoch.json()["allowed"] is True
        bridge._credential_generation += 2
        next_epoch = client.get(
            "/api/__generation_probe",
            headers={"Authorization": "Bearer " + token},
        )
        assert next_epoch.status_code == 200
        assert next_epoch.json()["captured"] == next_epoch.json()["current"]
        assert next_epoch.json()["current"] == first_epoch.json()["current"] + 2
        assert next_epoch.json()["allowed"] is True
        ok("real HTTP middleware snapshots the credential generation on every request")

        # An administrator may test a private HA address, but never the cloud
        # metadata service, localhost, credential-bearing or non-HTTP URLs.
        from types import SimpleNamespace
        resolve = bridge._resolve_ha_credentials
        assert resolve(SimpleNamespace(
            url="http://homeassistant:8123", token="valid-demo-ha-token"
        )) == ("http://homeassistant:8123", "valid-demo-ha-token")
        for hostile_url in (
            "file:///etc/passwd", "http://169.254.169.254/latest/meta-data",
            "http://localhost:8123", "http://127.0.0.1:8123",
            "http://user:pass@homeassistant:8123", "http://homeassistant/?token=secret",
            "http://[::1]:8123",
        ):
            try:
                resolve(SimpleNamespace(url=hostile_url, token="testing"))
            except ValueError:
                pass
            else:
                raise AssertionError("unsafe HA URL was accepted: " + hostile_url)
        ok("HA connection tests accept private HA while rejecting local metadata and unsafe URLs")

        # ── the access token survives a reload (persisted under /data) ──
        reloaded = load_bridge(root)
        assert reloaded.ACCESS_TOKEN == token, "access token must persist across restarts"
        ok("access token persists across restarts")
    finally:
        import logging

        os.chdir(previous_cwd)
        logging.shutdown()
        shutil.rmtree(temp_dir, ignore_errors=True)

    for line in results:
        print(f"  ok  {line}")
    print(f"api auth tests passed ({len(results)} groups)")


if __name__ == "__main__":
    main()
