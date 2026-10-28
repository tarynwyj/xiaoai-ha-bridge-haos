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
        assert client.post("/api/logout", headers=UI_HEADER).status_code == 200
        client.cookies.clear()
        assert client.get("/api/status").status_code == 401
        ok("logout invalidates the direct session")

        # ── E: explicit credential clear removes files and stored cookie ──
        client.post("/api/session", json_body={"token": token})
        cleared = client.post("/api/xiaomi/clear-login", headers=UI_HEADER)
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["ok"] is True
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
