#!/usr/bin/env python3
"""HTTP-level integration tests for the schedules API against Home Assistant.

Default (offline) mode starts a real local HTTP server
(``http.server.ThreadingHTTPServer`` on 127.0.0.1:0) that speaks the slice of the
HA REST contract the bridge depends on: the ping, states, services, the
automation config store, reload, turn_on/turn_off and the template API. The
*built* bridge.py is then loaded into a throw-away /data tree and driven through
a minimal in-process ASGI client (the same client test_api_auth.py uses -- no
httpx and no starlette TestClient), so the suite runs with runtime deps only.

Checks: create a repeating task, create a one-shot task, list both, edit one in
place, pause/resume through the real HA entity id, rooms/areas, delete, "still
there after a restart" (module re-import, same /data), and the automatic cleanup
of a one-shot task that already fired (an expired one that never fired is kept).

Optional real-HA mode (``--ha-url`` + ``--ha-token``) skips the fake server and
runs the same read-only/revertible checks against a live instance. Every task it
creates starts with ``xiaoai_bridge_`` and is removed at the end; automations
from any other source are never touched. Prefer the ``XIAOAI_TEST_HA_TOKEN``
environment variable over ``--ha-token`` so the token never lands in a shell
history.

Temporary files live only in the git-ignored ``.git/xiaoai-ci/http-work/`` when
this file runs from the checkout (a copy placed outside a checkout, e.g. inside
the built image, falls back to the platform temp dir).

Usage:
    python xiaoai_bridge/test_schedule_http.py <built bridge.py>
    python xiaoai_bridge/test_schedule_http.py <built bridge.py> \\
        --ha-url http://homeassistant:8123 --ha-token "$XIAOAI_TEST_HA_TOKEN"
"""

from __future__ import annotations

import asyncio
import hmac
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

try:  # Windows consoles default to a legacy code page; keep the output readable.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover - only relevant on very old interpreters
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent
# Temporary files must stay inside this (git-ignored) scratch directory.
WORK_ROOT = REPO_ROOT / ".git" / "xiaoai-ci" / "http-work"


def scratch_root() -> Path:
    """Where throw-away trees go.

    Inside a checkout everything temporary stays under the git-ignored
    ``.git/xiaoai-ci/http-work/``. When this file is copied out of the checkout
    (e.g. into an image as ``/tmp/test_schedule_http.py``) there is no such
    directory to write to, so the platform temp dir is used instead.
    """
    if (REPO_ROOT / ".git").is_dir():
        root = WORK_ROOT
    else:
        root = Path(tempfile.gettempdir()) / "xiaoai-ci" / "http-work"
    root.mkdir(parents=True, exist_ok=True)
    return root

# Synthetic credentials only: the fake HA checks them, nothing leaves the box.
FAKE_HA_TOKEN = "test-ha-token-not-real"
FAKE_ACCESS_HINT = "test-password"
UI_HEADER = {"X-Requested-With": "xiaoai-bridge"}
AUTOMATION_PREFIX = "xiaoai_bridge_"

CONFIG_SEED = {
    "xiaomi": {
        "username": "test-user",
        "password": FAKE_ACCESS_HINT,
        "device_did": "speaker-1",
        "hardware": "L05C",
        "tts_reply": True,
    },
    "homeassistant": {"url": "http://homeassistant.invalid:8123", "token": "unused"},
    "openai": {
        "api_base": "https://api.example.invalid",
        "api_key": "sk-test-not-a-real-key",
        "model": "test-model",
    },
    "ai_mode": False,
    "poll_interval_seconds": 2,
    "intent_rules": [],
}

# The service catalogue HA exposes; the bridge validates domain/service and the
# target entity against it before it writes an automation.
SERVICE_CATALOG = [
    {"domain": "light", "services": {
        "turn_on": {"fields": {"brightness": {"required": False},
                               "transition": {"required": False}}},
        "turn_off": {"fields": {"transition": {"required": False}}},
        "toggle": {"fields": {}},
    }},
    {"domain": "switch", "services": {
        "turn_on": {"fields": {}},
        "turn_off": {"fields": {}},
        "toggle": {"fields": {}},
    }},
    {"domain": "automation", "services": {
        "turn_on": {"fields": {}},
        "turn_off": {"fields": {}},
        "reload": {"fields": {}},
        "trigger": {"fields": {}},
    }},
    {"domain": "homeassistant", "services": {
        "turn_on": {"fields": {}},
        "turn_off": {"fields": {}},
        "restart": {"fields": {}},
    }},
    {"domain": "notify", "services": {"notify": {"fields": {}}}},
]


# ─────────────────────────── minimal ASGI test client ───────────────────────────
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

    def put(self, path, **kwargs):
        return self.request("PUT", path, **kwargs)

    def delete(self, path, **kwargs):
        return self.request("DELETE", path, **kwargs)


# ─────────────────────────── fake Home Assistant ───────────────────────────
class FakeHomeAssistant:
    """The slice of the HA REST contract the schedules API relies on.

    Configs are stored like HA's ``.storage`` automations: writing a config does
    NOT create an entity, ``automation/reload`` does. The generated entity id is
    a slug of the alias, which is exactly why an alias like
    ``小爱桥接-07:30 light.kitchen`` never produces ``automation.<config id>``.
    """

    def __init__(self, token: str):
        self.token = token
        self.lock = threading.Lock()
        self.requests = []        # every HTTP call: method/path/authorized
        self.service_calls = []   # parsed service payloads: domain/service/data
        self.configs = {}         # HA automation config storage: id -> config
        self.last_triggered = {}  # config id -> ISO timestamp
        self.devices = {
            "light.kitchen": ("off", "厨房灯"),
            "light.study_desk": ("on", "书房台灯"),
            "switch.study_light": ("off", "书房顶灯"),
            "sensor.living_room_temperature": ("21.5", "客厅温度"),
        }
        self.areas = [
            {"id": "kitchen", "name": "厨房",
             "entities": ["light.kitchen", "switch.kitchen_plug"]},
            {"id": "study", "name": "书房",
             "entities": ["light.study_desk", "switch.study_light"]},
        ]
        self.states = {}
        with self.lock:
            self._rebuild_states()

    # -- helpers shared by the handler and the tests -------------------------
    @staticmethod
    def entity_id_for(alias) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", str(alias or "").lower()).strip("_")
        return "automation." + (slug or "task")

    def _rebuild_states(self) -> None:
        states = {}
        for entity_id, (state, name) in self.devices.items():
            states[entity_id] = {
                "entity_id": entity_id,
                "state": state,
                "attributes": {"friendly_name": name},
            }
        for config_id, config in self.configs.items():
            entity_id = self.entity_id_for(config.get("alias") or config_id)
            states[entity_id] = {
                "entity_id": entity_id,
                "state": "on",
                "attributes": {
                    "id": config_id,
                    "friendly_name": config.get("alias") or config_id,
                    "last_triggered": self.last_triggered.get(config_id),
                },
            }
        self.states = states

    # -- public API used by the HTTP handler --------------------------------
    def snapshot_states(self):
        with self.lock:
            return [dict(state, attributes=dict(state["attributes"]))
                    for state in self.states.values()]

    def get_state(self, entity_id):
        with self.lock:
            state = self.states.get(entity_id)
            return dict(state, attributes=dict(state["attributes"])) if state else None

    def put_config(self, config_id, config):
        with self.lock:
            self.configs[config_id] = config

    def get_config(self, config_id):
        with self.lock:
            return self.configs.get(config_id)

    def delete_config(self, config_id) -> bool:
        with self.lock:
            return self.configs.pop(config_id, None) is not None

    def reload_automations(self):
        with self.lock:
            self._rebuild_states()

    def set_last_triggered(self, config_id, value):
        """Test hook: pretend HA ran this config (None = never ran)."""
        with self.lock:
            if value is None:
                self.last_triggered.pop(config_id, None)
            else:
                self.last_triggered[config_id] = value
            self._rebuild_states()

    def record_service(self, domain, service, data):
        with self.lock:
            self.service_calls.append(
                {"domain": domain, "service": service, "data": data})
            if domain == "automation" and service in ("turn_on", "turn_off"):
                entity_id = ""
                if isinstance(data, dict):
                    entity_id = str(data.get("entity_id") or "")
                state = self.states.get(entity_id)
                if state is not None:
                    state["state"] = "on" if service == "turn_on" else "off"


class FakeHAHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeHA/1.0"

    def log_message(self, fmt, *args):  # keep the test output deterministic
        pass

    @property
    def store(self) -> FakeHomeAssistant:
        return self.server.store

    # -- plumbing -----------------------------------------------------------
    def _record(self, authorized: bool) -> None:
        self.store.requests.append({
            "method": self.command,
            "path": urlsplit(self.path).path,
            "authorized": bool(authorized),
        })

    def _allowed(self) -> bool:
        header = self.headers.get("Authorization") or ""
        return hmac.compare_digest(header, "Bearer " + self.store.token)

    def _gate(self) -> bool:
        if self._allowed():
            self._record(True)
            return True
        self._record(False)
        self._send_json(401, {"message": "Unauthorized"})
        return False

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8")) or {}
        except ValueError:
            return {}

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status, payload):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    # -- routes -------------------------------------------------------------
    def do_GET(self):
        if not self._gate():
            return
        path = urlsplit(self.path).path
        if path == "/api/":
            return self._send_json(200, {"message": "API running"})
        if path == "/api/config":
            return self._send_json(200, {"time_zone": getattr(self.store, "time_zone", "UTC")})
        if path == "/api/states":
            status = getattr(self.store, "states_status", 200)
            if status != 200:
                return self._send_json(status, {"message": "temporary HA state query failure"})
            if getattr(self.store, "malformed_states", False):
                return self._send_json(200, {"unexpected": "payload"})
            return self._send_json(200, self.store.snapshot_states())
        if path.startswith("/api/states/"):
            entity_id = unquote(path[len("/api/states/"):])
            state = self.store.get_state(entity_id)
            if state is None:
                return self._send_json(404, {"message": "Entity not found."})
            return self._send_json(200, state)
        if path == "/api/services":
            return self._send_json(200, SERVICE_CATALOG)
        if path.startswith("/api/config/automation/config/"):
            config_id = unquote(path[len("/api/config/automation/config/"):])
            config = self.store.get_config(config_id)
            if config is None:
                return self._send_json(404, {"message": "Not found"})
            return self._send_json(200, config)
        return self._send_json(404, {"message": "Not found"})

    def do_POST(self):
        if not self._gate():
            return
        path = urlsplit(self.path).path
        payload = self._read_json()
        if path == "/api/template":
            # HA renders the template and returns the rendered text: the bridge
            # therefore reads the body as text and json.loads() it once.
            body = json.dumps(self.store.areas, ensure_ascii=False)
            return self._send(200, body.encode("utf-8"))
        if path.startswith("/api/config/automation/config/"):
            config_id = unquote(path[len("/api/config/automation/config/"):])
            for action in (payload or {}).get("action", []):
                target = (action.get("target") or {}).get("entity_id", [])
                ids = target.split(",") if isinstance(target, str) else target
                if not isinstance(ids, list) or any(
                        not isinstance(eid, str) or not re.fullmatch(r"[a-z_][a-z0-9_]*\.[a-z0-9_]+", eid.strip())
                        for eid in ids):
                    return self._send_json(400, {"message": "Message malformed: invalid entity_id"})
            self.store.put_config(config_id, payload or {})
            return self._send_json(200, {"result": "ok"})
        if path == "/api/services/automation/reload":
            self.store.record_service("automation", "reload", payload)
            status = getattr(self.store, "reload_status", 200)
            if status != 200:
                return self._send_json(status, {"message": "injected reload failure"})
            self.store.reload_automations()
            return self._send_json(200, [])
        if path.startswith("/api/services/"):
            parts = path.split("/")
            if len(parts) >= 5:
                self.store.record_service(parts[3], parts[4], payload)
                return self._send_json(200, [])
        return self._send_json(404, {"message": "Not found"})

    def do_DELETE(self):
        if not self._gate():
            return
        path = urlsplit(self.path).path
        if path.startswith("/api/config/automation/config/"):
            config_id = unquote(path[len("/api/config/automation/config/"):])
            if self.store.delete_config(config_id):
                return self._send_json(200, {"result": "ok"})
            return self._send_json(404, {"message": "Not found"})
        return self._send_json(404, {"message": "Not found"})


class FakeHAServer(ThreadingHTTPServer):
    """Threaded fake HA; aiohttp closing keep-alive sockets is not a test error."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionResetError, ConnectionAbortedError,
                              BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def start_fake_ha(token: str):
    """Serve the fake HA on an OS-assigned port (0) in a daemon thread."""
    server = FakeHAServer(("127.0.0.1", 0), FakeHAHandler)
    server.store = FakeHomeAssistant(token)
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.02},
                              name="fake-ha", daemon=True)
    thread.start()
    return server


def http_json(base, path, method="GET", payload=None, token=FAKE_HA_TOKEN,
              headers=None):
    """Call the (fake or real) HA REST API with the stdlib only."""
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if token is not None:
        request_headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(base.rstrip("/") + path, data=data,
                                     headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


# ─────────────────────────── bridged runtime ───────────────────────────
def build_runtime(root: Path, bridge_source: Path, ha_url: str, ha_token: str) -> Path:
    """Create a /data-like tree (config/, logs/, web/) next to a copy of bridge.py."""
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "web").mkdir(parents=True, exist_ok=True)
    shutil.copy2(bridge_source, root / "bridge.py")
    config = json.loads(json.dumps(CONFIG_SEED, ensure_ascii=False))
    config["homeassistant"] = {"url": ha_url, "token": ha_token}
    (root / "config" / "config.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return root


def load_bridge(root: Path, module_name: str):
    """Import the patched bridge.py with ``root`` as the process working directory."""
    previous = os.getcwd()
    os.chdir(root)
    try:
        spec = importlib.util.spec_from_file_location(module_name, root / "bridge.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    finally:
        os.chdir(previous)
    # The bridge logs every creation on the console at INFO; keep the report clean.
    logging.getLogger(module_name).setLevel(logging.WARNING)
    return module


def login(root: Path, bridge):
    """Log in with the generated direct-access token; return (client, token)."""
    token = (root / "config" / ".access_token").read_text(encoding="utf-8").strip()
    assert len(token) >= 32, "access token must be long and random"
    client = Client(bridge.app)
    response = client.post("/api/session", json_body={"token": token})
    assert response.status_code == 200 and response.json().get("ok") is True, (
        "direct login failed: %s" % response.text[:200])
    return client, token


def registry_path(root: Path) -> Path:
    return root / "config" / "schedules.json"


def read_registry(root: Path) -> list:
    path = registry_path(root)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def registry_entry(root: Path, config_id: str):
    for item in read_registry(root):
        if str(item.get("id")) == config_id:
            return item
    return None


def state_config_ids(store) -> set:
    return {str(state["attributes"].get("id")) for state in store.snapshot_states()
            if state["attributes"].get("id")}


def rows_by_config_id(listed) -> dict:
    return {str(row.get("config_id")): row for row in listed or []}


# ─────────────────────────── real HA (optional) ───────────────────────────
def run_real_ha(bridge_source: Path, root: Path, url: str, token: str,
                check, eq):
    """Same assertions against a live HA; every created task is removed again."""
    created = []
    previous_cwd = os.getcwd()
    try:
        build_runtime(root, bridge_source, url, token)
        os.chdir(root)

        try:
            status, _ping = http_json(url, "/api/", token=token)
        except Exception as error:
            check("real HA: /api/ answers the ping", False,
                  type(error).__name__ + " while reaching " + url)
            return created
        eq("real HA: /api/ answers the ping", status, 200)

        bridge = load_bridge(root, "patched_bridge_http")
        client, _access = login(root, bridge)

        baseline = client.get("/api/automations")
        check("real HA: the bridge lists automations",
              baseline.status_code == 200 and baseline.json().get("ok") is True,
              baseline.text[:200])

        status, raw = http_json(url, "/api/states", token=token)
        eq("real HA: /api/states answers", status, 200)
        try:
            states = json.loads(raw or b"[]")
        except ValueError:
            states = []
        target = ""
        for domain in ("light", "switch"):
            for state in states if isinstance(states, list) else []:
                entity_id = str(state.get("entity_id") or "")
                if entity_id.startswith(domain + "."):
                    target = entity_id
                    break
            if target:
                break
        check("real HA: a light or switch entity can be used as the target",
              bool(target), "no light/switch entity on this instance")
        if not target:
            return created
        action_domain = target.split(".", 1)[0]

        created_resp = client.post("/api/automations", headers=UI_HEADER, json_body={
            "time": "03:17", "days": "mon", "domain": action_domain,
            "service": "turn_on", "entity_id": target})
        eq("real HA: creating a repeating task succeeds", created_resp.status_code, 200)
        config_id = str(created_resp.json().get("automation_id") or "")
        check("real HA: the new task uses the bridge prefix",
              config_id.startswith(AUTOMATION_PREFIX), config_id)
        if config_id:
            created.append(config_id)
        if not config_id:
            return created

        status, raw = http_json(url, "/api/config/automation/config/" + config_id,
                                token=token)
        eq("real HA: HA stores the new automation config", status, 200)
        stored = json.loads(raw or b"{}")
        eq("real HA: HA stored the expected trigger", stored.get("trigger"),
           [{"platform": "time", "at": "03:17:00"}])
        eq("real HA: HA stored the expected action", stored.get("action"),
           [{"service": action_domain + ".turn_on",
             "target": {"entity_id": target}}])

        rows = rows_by_config_id(
            client.get("/api/automations").json().get("automations"))
        row = rows.get(config_id) or {}
        check("real HA: the task is listed with a next run",
              row.get("status") in ("active", "disabled") and bool(row.get("next_run")),
              row)

        paused = client.post("/api/automations/%s/toggle" % config_id,
                             headers=UI_HEADER, json_body={"enabled": False})
        eq("real HA: pausing the task succeeds", paused.status_code, 200)
        entity_id = str(row.get("entity_id") or "")
        status, raw = http_json(url, "/api/states/" + entity_id, token=token)
        eq("real HA: the paused entity is off",
           json.loads(raw or b"{}").get("state"), "off")

        resumed = client.post("/api/automations/%s/toggle" % config_id,
                              headers=UI_HEADER, json_body={"enabled": True})
        eq("real HA: resuming the task succeeds", resumed.status_code, 200)
        status, raw = http_json(url, "/api/states/" + entity_id, token=token)
        eq("real HA: the resumed entity is on",
           json.loads(raw or b"{}").get("state"), "on")

        edited = client.put("/api/automations/" + config_id, headers=UI_HEADER,
                            json_body={"time": "03:19", "days": "mon",
                                       "domain": action_domain,
                                       "service": "turn_on", "entity_id": target})
        eq("real HA: editing the task in place succeeds", edited.status_code, 200)
        status, raw = http_json(url, "/api/config/automation/config/" + config_id,
                                token=token)
        eq("real HA: HA holds the edited trigger",
           json.loads(raw or b"{}").get("trigger"),
           [{"platform": "time", "at": "03:19:00"}])

        removed = client.delete("/api/automations/" + config_id, headers=UI_HEADER)
        eq("real HA: deleting the task succeeds", removed.status_code, 200)
        status, _raw = http_json(url, "/api/config/automation/config/" + config_id,
                                 token=token)
        eq("real HA: the config is gone from HA", status, 404)
        listed = rows_by_config_id(
            client.get("/api/automations").json().get("automations"))
        check("real HA: the task is gone from the list", config_id not in listed,
              sorted(listed))
    finally:
        # Revert everything this run created; never touch foreign automations.
        for config_id in created:
            try:
                http_json(url, "/api/config/automation/config/" + config_id,
                          "DELETE", token=token)
            except Exception:
                pass
        os.chdir(previous_cwd)
    return created


# ─────────────────────────── offline suite ───────────────────────────
def run_fake_ha(root: Path, base: str, store, check, eq):
    """Drive the built bridge against the fake HA and assert both sides."""
    os.chdir(root)
    bridge = load_bridge(root, "patched_bridge_http")
    client, access_token = login(root, bridge)

    # ── A: the fake HA itself honours the contract we rely on ──
    status, raw = http_json(base, "/api/")
    eq("fake HA: /api/ answers the ping",
       (status, json.loads(raw or b"{}")), (200, {"message": "API running"}))

    probe_status, _probe = http_json(base, "/api/", token="definitely-wrong-token")
    eq("fake HA: a wrong Bearer token is refused", probe_status, 401)
    missing_status, _missing = http_json(base, "/api/states", token=None)
    eq("fake HA: a missing Bearer token is refused", missing_status, 401)

    denied_id = "xiaoai_bridge_denied_probe"
    denied_status, _denied = http_json(
        base, "/api/config/automation/config/" + denied_id, "POST",
        {"alias": "denied"}, token="definitely-wrong-token")
    eq("fake HA: a wrong token cannot write an automation config", denied_status, 401)
    check("fake HA: the refused write never reached the store",
          denied_id not in store.configs)

    probe_id = "xiaoai_bridge_contract_probe"
    probe_config = {
        "alias": "contract probe",
        "trigger": [{"platform": "time", "at": "05:00:00"}],
        "action": [{"service": "light.turn_on",
                    "target": {"entity_id": "light.kitchen"}}],
    }
    created_status, _created = http_json(
        base, "/api/config/automation/config/" + probe_id, "POST", probe_config)
    eq("fake HA: an automation config can be stored", created_status, 200)
    read_status, read_raw = http_json(
        base, "/api/config/automation/config/" + probe_id)
    eq("fake HA: a stored config can be read back",
       (read_status, json.loads(read_raw or b"{}")), (200, probe_config))
    missing_cfg_status, _missing_cfg = http_json(
        base, "/api/config/automation/config/xiaoai_bridge_contract_missing")
    eq("fake HA: an unknown config id answers 404", missing_cfg_status, 404)

    reload_status, _reload = http_json(base, "/api/services/automation/reload", "POST", {})
    eq("fake HA: automation/reload is accepted", reload_status, 200)
    probe_entity = store.entity_id_for("contract probe")
    state = store.get_state(probe_entity) or {}
    eq("fake HA: reload turns a stored config into an automation entity",
       (state.get("state"), state.get("attributes", {}).get("id")),
       ("on", probe_id))
    check("fake HA: the automation entity carries friendly_name and last_triggered",
          state.get("attributes", {}).get("friendly_name") == "contract probe"
          and "last_triggered" in (state.get("attributes") or {}),
          state.get("attributes"))

    services_status, services_raw = http_json(base, "/api/services")
    catalog = {}
    if services_status == 200:
        for item in json.loads(services_raw or b"[]"):
            if isinstance(item, dict) and item.get("domain"):
                catalog[str(item["domain"])] = item.get("services") or {}
    check("fake HA: light/switch/automation/homeassistant are offered",
          {"light", "switch", "automation", "homeassistant"} <= set(catalog),
          sorted(catalog))
    check("fake HA: automation exposes reload/turn_on/turn_off",
          {"reload", "turn_on", "turn_off"} <= set(catalog.get("automation") or {}),
          sorted(catalog.get("automation") or {}))

    http_json(base, "/api/services/automation/turn_off", "POST",
              {"entity_id": probe_entity})
    off_state = (store.get_state(probe_entity) or {}).get("state")
    eq("fake HA: automation.turn_off flips the entity state", off_state, "off")
    http_json(base, "/api/services/automation/turn_on", "POST",
              {"entity_id": probe_entity})
    on_state = (store.get_state(probe_entity) or {}).get("state")
    eq("fake HA: automation.turn_on flips it back", on_state, "on")

    template_status, template_raw = http_json(
        base, "/api/template", "POST", {"template": "unused"})
    eq("fake HA: the template API answers", template_status, 200)
    try:
        areas = json.loads(template_raw or b"[]")
    except ValueError:
        areas = None
    check("fake HA: /api/template returns the rendered JSON as text",
          isinstance(areas, list), template_raw[:120])
    check("fake HA: the template lists rooms with their entities",
          isinstance(areas, list) and bool(areas)
          and areas[0].get("id") == "kitchen"
          and areas[0].get("name") == "厨房"
          and "light.kitchen" in (areas[0].get("entities") or []),
          areas)

    delete_status, _delete = http_json(
        base, "/api/config/automation/config/" + probe_id, "DELETE")
    eq("fake HA: a config can be deleted", delete_status, 200)
    gone_status, _gone = http_json(
        base, "/api/config/automation/config/" + probe_id)
    eq("fake HA: the deleted config answers 404", gone_status, 404)
    http_json(base, "/api/services/automation/reload", "POST", {})
    check("fake HA: reload drops the entity of a deleted config",
          store.get_state(probe_entity) is None)

    recorded = [(item["method"], item["path"]) for item in store.requests]
    check("fake HA: every request is recorded",
          ("GET", "/api/") in recorded
          and ("DELETE", "/api/config/automation/config/" + probe_id) in recorded,
          recorded[:6])

    # ── B: the built bridge drives that contract ──
    unauthorized = call(bridge.app, "GET", "/api/automations")
    eq("bridge: /api/automations still requires authentication",
       unauthorized.status_code, 401)
    check("bridge: the registry path stays under the /data tree",
          Path(bridge._schedule_registry_path()).resolve() == registry_path(root).resolve(),
          str(bridge._schedule_registry_path()))

    periodic_resp = client.post("/api/automations", headers=UI_HEADER, json_body={
        "time": "07:30", "days": "mon,wed,fri", "domain": "light",
        "service": "turn_on", "entity_id": "light.kitchen"})
    eq("bridge: creating a repeating task returns 200", periodic_resp.status_code, 200)
    periodic_body = periodic_resp.json()
    periodic_id = str(periodic_body.get("automation_id") or "")
    check("bridge: the created task carries a bridge config id",
          periodic_body.get("ok") is True and periodic_id.startswith(AUTOMATION_PREFIX),
          periodic_body)

    periodic_config = store.get_config(periodic_id) or {}
    eq("bridge: the repeating task was written to HA",
       sorted(store.configs), [periodic_id])
    eq("bridge: the alias names the time and the target", periodic_config.get("alias"),
       "小爱桥接-07:30 light.kitchen")
    eq("bridge: the trigger is a clock time", periodic_config.get("trigger"),
       [{"platform": "time", "at": "07:30:00"}])
    eq("bridge: the weekday condition is HA's time condition",
       periodic_config.get("condition"),
       [{"condition": "time", "weekday": ["mon", "wed", "fri"]}])
    eq("bridge: the action calls the requested service on the entity",
       periodic_config.get("action"),
       [{"service": "light.turn_on", "target": {"entity_id": "light.kitchen"}}])
    eq("bridge: single-run mode is set", periodic_config.get("mode"), "single")
    periodic_entry = registry_entry(root, periodic_id) or {}
    check("bridge: the repeating task is registered in /data/schedules.json",
          periodic_entry.get("time") == "07:30:00"
          and periodic_entry.get("weekdays") == ["mon", "wed", "fri"]
          and periodic_entry.get("entity_id") == "light.kitchen"
          and periodic_entry.get("domain") == "light"
          and periodic_entry.get("service") == "turn_on"
          and periodic_entry.get("source") == "form"
          and periodic_entry.get("once") is False, periodic_entry)

    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    once_resp = client.post("/api/automations", headers=UI_HEADER, json_body={
        "time": "21:15", "once": True, "date": tomorrow, "domain": "switch",
        "service": "turn_on", "entity_id": "switch.study_light"})
    eq("bridge: creating a one-shot task returns 200", once_resp.status_code, 200)
    once_body = once_resp.json()
    once_id = str(once_body.get("automation_id") or "")
    check("bridge: the one-shot task carries a bridge config id",
          once_body.get("ok") is True and once_id.startswith(AUTOMATION_PREFIX),
          once_body)
    once_config = store.get_config(once_id) or {}
    eq("bridge: the one-shot trigger is a clock time (HA time triggers have no date form)",
       once_config.get("trigger"),
       [{"platform": "time", "at": "21:15:00"}])
    eq("bridge: the one-shot guard is a date template", once_config.get("condition"),
       [{"condition": "template",
         "value_template": "{{ now().strftime('%Y-%m-%d') == '"
                           + tomorrow + "' }}"}])
    eq("bridge: the one-shot action targets the requested entity",
       once_config.get("action"),
       [{"service": "switch.turn_on", "target": {"entity_id": "switch.study_light"}}])
    once_entry = registry_entry(root, once_id) or {}
    check("bridge: the one-shot task records once/once_at",
          once_entry.get("once") is True
          and once_entry.get("once_at") == "%s 21:15" % tomorrow, once_entry)

    listed_resp = client.get("/api/automations")
    eq("bridge: listing the tasks returns 200", listed_resp.status_code, 200)
    rows = rows_by_config_id(listed_resp.json().get("automations"))
    eq("bridge: both tasks are listed", sorted(rows), sorted([periodic_id, once_id]))
    periodic_row = rows.get(periodic_id) or {}
    periodic_next = str(periodic_row.get("next_run") or "")
    check("bridge: the repeating row has next_run/status/source/once",
          bool(re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", periodic_next))
          and periodic_next.endswith(" 07:30")
          and periodic_row.get("status") == "active"
          and periodic_row.get("source") == "form"
          and periodic_row.get("once") is False, periodic_row)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", periodic_next):
        check("bridge: next_run lands on one of the chosen weekdays",
              datetime.strptime(periodic_next, "%Y-%m-%d %H:%M").weekday()
              in (0, 2, 4), periodic_next)
    periodic_entity = store.entity_id_for("小爱桥接-07:30 light.kitchen")
    eq("bridge: the list row points at the real HA entity id",
       periodic_row.get("entity_id"), periodic_entity)
    check("bridge: the HA entity id is not automation.<config id>",
          periodic_entity != "automation." + periodic_id
          and periodic_row.get("entity_id") != "automation." + periodic_id,
          periodic_entity)
    once_row = rows.get(once_id) or {}
    check("bridge: the one-shot row carries once/once_at/status",
          once_row.get("once") is True
          and once_row.get("once_at") == "%s 21:15" % tomorrow
          and once_row.get("status") == "active"
          and str(once_row.get("next_run") or "").startswith(tomorrow), once_row)

    # ── C: editing overwrites the same HA config, never adds a row ──
    before_ids = sorted(store.configs)
    edited_resp = client.put("/api/automations/" + periodic_id, headers=UI_HEADER,
                             json_body={"time": "08:45", "days": "mon,wed,fri",
                                        "domain": "light", "service": "turn_on",
                                        "entity_id": "light.kitchen"})
    eq("bridge: editing a task returns 200", edited_resp.status_code, 200)
    check("bridge: the edit reports the same automation id",
          edited_resp.json().get("automation_id") == periodic_id
          and edited_resp.json().get("updated") is True, edited_resp.json())
    eq("bridge: editing overwrites the same HA config id", sorted(store.configs),
       before_ids)
    edited_config = store.get_config(periodic_id) or {}
    eq("bridge: the edited trigger has the new time", edited_config.get("trigger"),
       [{"platform": "time", "at": "08:45:00"}])
    eq("bridge: the alias follows the new time", edited_config.get("alias"),
       "小爱桥接-08:45 light.kitchen")
    edited_entry = registry_entry(root, periodic_id) or {}
    check("bridge: the registry row is updated instead of duplicated",
          edited_entry.get("time") == "08:45:00"
          and len([item for item in read_registry(root)
                   if str(item.get("id")) == periodic_id]) == 1, edited_entry)
    rows_after_edit = rows_by_config_id(
        client.get("/api/automations").json().get("automations"))
    eq("bridge: editing adds no list row", len(rows_after_edit), 2)
    check("bridge: the edited row keeps the new time",
          str((rows_after_edit.get(periodic_id) or {}).get("next_run") or "")
          .endswith(" 08:45"), rows_after_edit.get(periodic_id))

    # ── D: pause/resume must address the HA entity, not the config id ──
    edited_entity = store.entity_id_for("小爱桥接-08:45 light.kitchen")
    paused_resp = client.post("/api/automations/%s/toggle" % periodic_id,
                              headers=UI_HEADER, json_body={"enabled": False})
    eq("bridge: pausing a task returns 200", paused_resp.status_code, 200)
    check("bridge: pausing reports enabled=false",
          paused_resp.json().get("enabled") is False, paused_resp.json())
    pause_call = store.service_calls[-1] if store.service_calls else {}
    check("bridge: pause calls automation.turn_off on the real entity id",
          pause_call.get("domain") == "automation"
          and pause_call.get("service") == "turn_off"
          and (pause_call.get("data") or {}).get("entity_id") == edited_entity,
          pause_call)
    eq("bridge: HA's state really changed to off",
       (store.get_state(edited_entity) or {}).get("state"), "off")
    paused_row = rows_by_config_id(
        client.get("/api/automations").json().get("automations")).get(periodic_id) or {}
    eq("bridge: the paused row is reported as disabled",
       paused_row.get("status"), "disabled")

    resumed_resp = client.post("/api/automations/%s/toggle" % periodic_id,
                               headers=UI_HEADER, json_body={"enabled": True})
    eq("bridge: resuming a task returns 200", resumed_resp.status_code, 200)
    resume_call = store.service_calls[-1] if store.service_calls else {}
    check("bridge: resume calls automation.turn_on on the real entity id",
          resume_call.get("service") == "turn_on"
          and (resume_call.get("data") or {}).get("entity_id") == edited_entity,
          resume_call)
    eq("bridge: HA's state really changed back to on",
       (store.get_state(edited_entity) or {}).get("state"), "on")

    # ── E: rooms/areas via the HA template API ──
    areas_resp = client.get("/api/ha/areas")
    eq("bridge: /api/ha/areas returns 200", areas_resp.status_code, 200)
    areas_body = areas_resp.json()
    check("bridge: the area list is reported as supported",
          areas_body.get("ok") is True and areas_body.get("supported") is True,
          areas_body)
    parsed_areas = {str(item.get("id")): item
                    for item in areas_body.get("areas") or []
                    if isinstance(item, dict)}
    check("bridge: rooms keep their names and entities",
          (parsed_areas.get("kitchen") or {}).get("name") == "厨房"
          and "light.kitchen" in ((parsed_areas.get("kitchen") or {})
                                  .get("entities") or [])
          and (parsed_areas.get("study") or {}).get("name") == "书房"
          and "light.study_desk" in ((parsed_areas.get("study") or {})
                                     .get("entities") or []), parsed_areas)

    # ── F: delete removes both the HA config and the list row ──
    foreign = client.delete("/api/automations/automation.foreign",
                            headers=UI_HEADER)
    eq("bridge: deleting a foreign automation is refused", foreign.status_code, 403)
    deleted_resp = client.delete("/api/automations/" + once_id, headers=UI_HEADER)
    eq("bridge: deleting a task returns 200", deleted_resp.status_code, 200)
    check("bridge: the delete reports ok", deleted_resp.json().get("ok") is True,
          deleted_resp.json())
    check("bridge: the deleted config is gone from HA", once_id not in store.configs)
    check("bridge: the deleted row is gone from the registry",
          registry_entry(root, once_id) is None)
    rows_after_delete = rows_by_config_id(
        client.get("/api/automations").json().get("automations"))
    eq("bridge: the list no longer contains the deleted task",
       sorted(rows_after_delete), [periodic_id])

    # ── G: "still there after a restart" (module re-import, same /data) ──
    sys.modules.pop("patched_bridge_http", None)
    restarted = load_bridge(root, "patched_bridge_http_restart")
    check("restart: the direct-access token survives the reload",
          restarted.ACCESS_TOKEN == access_token)
    client2, _access2 = login(root, restarted)
    restarted_resp = client2.get("/api/automations")
    eq("restart: the list still answers 200", restarted_resp.status_code, 200)
    restarted_rows = rows_by_config_id(
        restarted_resp.json().get("automations"))
    periodic_after_restart = restarted_rows.get(periodic_id) or {}
    check("restart: the repeating task is still listed",
          periodic_id in restarted_rows
          and periodic_after_restart.get("time") == "08:45:00"
          and periodic_after_restart.get("source") == "form",
          restarted_rows.get(periodic_id))
    check("restart: the row is backed by HA states and /data/schedules.json",
          state_config_ids(store) >= {periodic_id}
          and registry_entry(root, periodic_id) is not None,
          sorted(state_config_ids(store)))

    # ── H: one-shot tasks: fired ones are purged, unfired ones are kept ──
    later = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    purge_resp = client2.post("/api/automations", headers=UI_HEADER, json_body={
        "time": "06:05", "once": True, "date": tomorrow, "domain": "light",
        "service": "turn_on", "entity_id": "light.kitchen"})
    eq("cleanup: creating the to-be-fired one-shot task returns 200",
       purge_resp.status_code, 200)
    purge_id = str(purge_resp.json().get("automation_id") or "")
    keep_resp = client2.post("/api/automations", headers=UI_HEADER, json_body={
        "time": "06:20", "once": True, "date": later, "domain": "light",
        "service": "turn_on", "entity_id": "light.kitchen"})
    eq("cleanup: creating the never-fired one-shot task returns 200",
       keep_resp.status_code, 200)
    keep_id = str(keep_resp.json().get("automation_id") or "")
    check("cleanup: both one-shot tasks have bridge config ids",
          purge_id.startswith(AUTOMATION_PREFIX)
          and keep_id.startswith(AUTOMATION_PREFIX), [purge_id, keep_id])
    purge_alias = (store.get_config(purge_id) or {}).get("alias") or ""
    purge_entity = store.entity_id_for(purge_alias)

    past = (datetime.now() - timedelta(days=1))
    items = read_registry(root)
    seen_registry = set()
    for item in items:
        config_id = str(item.get("id"))
        seen_registry.add(config_id)
        if config_id in (purge_id, keep_id):
            item["once_at"] = past.strftime("%Y-%m-%d %H:%M")
    check("cleanup: both one-shot rows are in the registry",
          {purge_id, keep_id} <= seen_registry, sorted(seen_registry))
    registry_path(root).write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    store.set_last_triggered(purge_id, past.isoformat(timespec="seconds"))
    store.set_last_triggered(keep_id, None)

    before_cleanup = client2.get("/api/automations")
    eq("cleanup: GET is read-only and keeps completed configs", before_cleanup.status_code, 200)
    check("cleanup: reading the list does not remove completed tasks", purge_id in store.configs)
    cleanup_resp = client2.post("/api/automations/cleanup", headers=UI_HEADER, json_body={})
    eq("cleanup: explicit cleanup succeeds", cleanup_resp.status_code, 200)
    eq("cleanup: exactly one completed task is removed", cleanup_resp.json().get("removed"), 1)
    repeated_cleanup = client2.post("/api/automations/cleanup", headers=UI_HEADER, json_body={})
    eq("cleanup: a repeated cleanup is idempotent", repeated_cleanup.json().get("removed"), 0)
    cleaned_resp = client2.get("/api/automations")
    eq("cleanup: listing after the expiry still answers 200",
       cleaned_resp.status_code, 200)
    cleaned_rows = rows_by_config_id(cleaned_resp.json().get("automations"))
    check("cleanup: the fired one-shot task is purged from HA and the registry",
          purge_id not in cleaned_rows
          and purge_id not in store.configs
          and registry_entry(root, purge_id) is None, sorted(cleaned_rows))
    kept_row = cleaned_rows.get(keep_id) or {}
    check("cleanup: the expired one-shot task that never fired is kept",
          keep_id in cleaned_rows
          and kept_row.get("status") == "expired"
          and kept_row.get("expired") is True
          and keep_id in store.configs
          and registry_entry(root, keep_id) is not None, kept_row)
    check("cleanup: the repeating task is untouched",
          periodic_id in cleaned_rows
          and (store.get_config(periodic_id) or {}).get("trigger")
          == [{"platform": "time", "at": "08:45:00"}],
          cleaned_rows.get(periodic_id))
    check("cleanup: purging reloaded HA so the dead entity is gone",
          purge_entity not in {state["entity_id"]
                               for state in store.snapshot_states()},
          purge_entity)

    # Two concurrent cleanup calls must re-read after the lock, deleting only once.
    store.put_config(purge_id, {"alias": "concurrent cleanup", "trigger": [], "action": []})
    store.set_last_triggered(purge_id, past.isoformat(timespec="seconds"))
    restarted._schedule_registry_add(purge_id, past.strftime("%H:%M"), [], "light.kitchen",
                                    "light", "turn_on", "form", True,
                                    once_at=past.strftime("%Y-%m-%d %H:%M"))
    deletes_before = sum(x["method"] == "DELETE" and x["path"].endswith("/" + purge_id)
                         for x in store.requests)
    async def concurrent_cleanups():
        return await asyncio.gather(restarted.cleanup_automations(), restarted.cleanup_automations())
    outcomes = asyncio.run(concurrent_cleanups())
    eq("cleanup: concurrent calls collectively remove exactly one task",
       sum(x["removed"] for x in outcomes), 1)
    deletes_after = sum(x["method"] == "DELETE" and x["path"].endswith("/" + purge_id)
                        for x in store.requests)
    eq("cleanup: concurrent calls send only one HA DELETE", deletes_after - deletes_before, 1)

    # The form API must preserve a multi-target array all the way to HA storage.
    target_ids = ["light.kitchen", "light.study_desk"]
    multi = client2.post("/api/automations", headers=UI_HEADER, json_body={
        "time": "09:10", "domain": "homeassistant", "service": "turn_on",
        "entity_id": target_ids})
    eq("multi: array target request is accepted", multi.status_code, 200)
    mid = multi.json().get("automation_id")
    if multi.status_code == 200:
        eq("multi: final HA config retains the exact array",
           store.configs[mid]["action"][0]["target"]["entity_id"], target_ids)
        edited = client2.put("/api/automations/" + mid, headers=UI_HEADER, json_body={
            "time": "09:20", "domain": "homeassistant", "service": "turn_off",
            "entity_id": ",".join(target_ids)})
        eq("multi: comma-separated editing preserves both targets", edited.status_code, 200)
        eq("multi: edited config still contains an array",
           store.configs[mid]["action"][0]["target"]["entity_id"], target_ids)
        client2.delete("/api/automations/" + mid, headers=UI_HEADER)
    invalid = client2.post("/api/automations", headers=UI_HEADER, json_body={
        "time": "09:10", "domain": "homeassistant", "service": "turn_on", "entity_id": str(target_ids)})
    eq("multi: a Python-list string is rejected", invalid.status_code, 400)
    malformed_status, _ = http_json(base, "/api/config/automation/config/xiaoai_bridge_malformed",
        "POST", {"alias": "malformed", "action": [{"service": "light.turn_on",
                           "target": {"entity_id": str(target_ids)}}]})
    eq("multi: fake HA enforces the invalid-target contract too", malformed_status, 400)

    store.time_zone = "Asia/Shanghai"
    # monotonic() may be < 300 on a freshly booted CI runner; zero is not
    # necessarily older than the five-minute cache TTL.
    restarted._schedule_clock["at"] = time.monotonic() - 301
    client2.get("/api/automations")
    eq("clock: the real HTTP config endpoint supplies HA's timezone",
       str(restarted._schedule_clock["zone"]), "Asia/Shanghai")
    eq("clock: HA UTC trigger timestamps convert to HA's calendar date",
       restarted._schedule_local_naive("2030-11-08T18:30:00Z"), datetime(2030, 11, 9, 2, 30))
    store.time_zone = "No/SuchZone"
    restarted._schedule_clock.update(zone=None, at=0)
    unknown_clock = client2.post("/api/automations", headers=UI_HEADER, json_body={
        "time": "08:00", "once": True, "date": tomorrow, "domain": "light",
        "service": "turn_on", "entity_id": "light.kitchen"})
    eq("clock: unknown HA timezone refuses one-shots instead of guessing", unknown_clock.status_code, 503)
    store.time_zone = "UTC"
    restarted._schedule_clock.update(zone=None, at=0)

    # HA outages are unknown, never an authoritative empty automation list.
    before_outage = dict(store.configs)
    for status in (401, 503):
        store.states_status = status
        unavailable_list = client2.get("/api/automations")
        eq("outage: listing maps HA HTTP %s to unknown/502" % status,
           unavailable_list.status_code, 502)
        unavailable_toggle = client2.post("/api/automations/" + periodic_id + "/toggle",
                                          headers=UI_HEADER, json_body={"enabled": False})
        eq("outage: toggling maps HA HTTP %s to 502, never 404" % status,
           unavailable_toggle.status_code, 502)
        unavailable_cleanup = client2.post("/api/automations/cleanup", headers=UI_HEADER, json_body={})
        eq("outage: cleanup stops on unknown state", unavailable_cleanup.status_code, 502)
        eq("outage: no automation configuration is changed", store.configs, before_outage)
    store.states_status = 200
    store.malformed_states = True
    eq("outage: malformed HA states are unknown too", client2.get("/api/automations").status_code, 502)
    store.malformed_states = False
    eq("outage: listing recovers when HA answers again", client2.get("/api/automations").status_code, 200)

    # A corrupt registry must block EVERY mutation and preserve the original bytes.
    path = registry_path(root)
    original_registry = path.read_bytes()
    bad_payloads = (b'[{"id":"old-task"},', b'{"not":"a list"}', b'[null]',
                    b'[{"id":"duplicate"},{"id":"duplicate"}]')
    for bad in bad_payloads:
        path.write_bytes(bad)
        before_mutations = dict(store.configs)
        eq("registry: corrupt input makes listing unavailable", client2.get("/api/automations").status_code, 503)
        attempted = client2.post("/api/automations", headers=UI_HEADER, json_body={
            "time": "12:34", "domain": "light", "service": "turn_on", "entity_id": "light.kitchen"})
        eq("registry: creation refuses a corrupt registry", attempted.status_code, 503)
        edit = client2.put("/api/automations/" + periodic_id, headers=UI_HEADER, json_body={
            "time": "12:35", "domain": "light", "service": "turn_off", "entity_id": "light.kitchen"})
        eq("registry: editing refuses a corrupt registry", edit.status_code, 503)
        deletion = client2.delete("/api/automations/" + periodic_id, headers=UI_HEADER)
        eq("registry: deletion refuses a corrupt registry", deletion.status_code, 503)
        cleanup = client2.post("/api/automations/cleanup", headers=UI_HEADER, json_body={})
        eq("registry: cleanup refuses a corrupt registry", cleanup.status_code, 503)
        eq("registry: no HA write/delete occurred", store.configs, before_mutations)
        eq("registry: corrupt bytes are preserved exactly", path.read_bytes(), bad)
    path.write_bytes(original_registry)
    eq("registry: restoring the original registry recovers listing", client2.get("/api/automations").status_code, 200)

    # The HA DELETE and local registry update can succeed independently.
    original_write = restarted._schedule_registry_write
    def fail_registry_write(_rows):
        raise restarted.ScheduleRegistryError("injected registry write failure")
    for reload_status in (200, 503):
        created = client2.post("/api/automations", headers=UI_HEADER, json_body={
            "time": "12:37", "domain": "light", "service": "turn_on", "entity_id": "light.kitchen"})
        eq("partial delete: fixture creation succeeds", created.status_code, 200)
        partial_id = created.json()["automation_id"]
        original_bytes = path.read_bytes()
        restarted._schedule_registry_write = fail_registry_write
        store.reload_status = reload_status
        try:
            response = client2.delete("/api/automations/" + partial_id, headers=UI_HEADER)
            body = response.json()
            eq("partial delete: acknowledged HA deletion returns 200", response.status_code, 200)
            check("partial delete: structured outcome distinguishes local failure",
                  body.get("ok") is True and body.get("ha_deleted") is True
                  and body.get("registry_updated") is False, body)
            eq("partial delete: reload result is accurate", body.get("reloaded"), reload_status == 200)
            check("partial delete: message describes deletion rather than saving",
                  "已删除" in body.get("msg", "") and "登记" in body.get("msg", "")
                  and "保存" not in body.get("msg", ""), body)
            if reload_status != 200:
                check("partial delete: warns that runtime may still be active", "仍在运行" in body.get("msg", ""), body)
            check("partial delete: HA config is actually gone", partial_id not in store.configs)
            eq("partial delete: registry bytes are preserved", path.read_bytes(), original_bytes)
        finally:
            restarted._schedule_registry_write = original_write
            store.reload_status = 200
        retry = client2.delete("/api/automations/" + partial_id, headers=UI_HEADER)
        eq("partial delete: retry clears local remnants even when HA config is missing", retry.status_code, 200)
        check("partial delete: retry removes the registry row", registry_entry(root, partial_id) is None)

    store.put_config(purge_id, {"alias": "partial cleanup", "trigger": [], "action": []})
    store.set_last_triggered(purge_id, past.isoformat(timespec="seconds"))
    restarted._schedule_registry_add(purge_id, past.strftime("%H:%M"), [], "light.kitchen",
                                    "light", "turn_on", "form", True,
                                    once_at=past.strftime("%Y-%m-%d %H:%M"))
    original_bytes = path.read_bytes()
    restarted._schedule_registry_write = fail_registry_write
    try:
        partial = client2.post("/api/automations/cleanup", headers=UI_HEADER, json_body={})
        eq("partial cleanup: HA deletion is acknowledged", partial.status_code, 200)
        eq("partial cleanup: pending registry row is reported", partial.json().get("registry_pending"), 1)
        eq("partial cleanup: no fully cleaned row is claimed", partial.json().get("removed"), 0)
        check("partial cleanup: message describes the completed HA deletion", "已删除" in partial.json().get("msg", ""))
        check("partial cleanup: HA config is gone", purge_id not in store.configs)
        eq("partial cleanup: registry bytes are retained", path.read_bytes(), original_bytes)
    finally:
        restarted._schedule_registry_write = original_write
    eq("partial cleanup: individual delete can finish the cleanup",
       client2.delete("/api/automations/" + purge_id, headers=UI_HEADER).status_code, 200)

    # ── I: nothing reached HA's config store without the configured token ──
    denied_path = "/api/config/automation/config/" + denied_id
    config_writes = [item for item in store.requests
                     if item["method"] in ("POST", "DELETE")
                     and "/api/config/automation/config/" in item["path"]
                     and item["path"] != denied_path]
    check("bridge: every automation config write carried the configured token",
          bool(config_writes)
          and all(item["authorized"] for item in config_writes),
          [item for item in config_writes if not item["authorized"]])


# ─────────────────────────── entry point ───────────────────────────
def parse_args(argv):
    if not argv:
        raise SystemExit(
            "usage: python xiaoai_bridge/test_schedule_http.py <built bridge.py> "
            "[--ha-url URL [--ha-token TOKEN]]")
    bridge_path = Path(argv[0]).expanduser()
    ha_url = None
    ha_token = os.environ.get("XIAOAI_TEST_HA_TOKEN") or None
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--ha-url" and index + 1 < len(argv):
            ha_url = argv[index + 1]
            index += 2
        elif arg == "--ha-token" and index + 1 < len(argv):
            ha_token = argv[index + 1]
            index += 2
        else:
            raise SystemExit("unknown argument: " + arg)
    if ha_url and not ha_token:
        raise SystemExit("--ha-url needs --ha-token (or XIAOAI_TEST_HA_TOKEN)")
    return bridge_path, ha_url, ha_token


def main(argv=None):
    bridge_path, ha_url, ha_token = parse_args(
        sys.argv[1:] if argv is None else argv)
    bridge_source = bridge_path.resolve()
    assert bridge_source.exists(), f"built bridge.py not found at {bridge_source}"
    try:
        yaml.safe_load("probe: 1")
    except Exception:  # pragma: no cover - PyYAML ships with the add-on
        raise SystemExit("PyYAML is required: pip install pyyaml")

    temp_dir = Path(tempfile.mkdtemp(prefix="run-", dir=str(scratch_root())))
    previous_cwd = os.getcwd()
    checks = 0
    passed = []
    failures = []

    def check(name, condition, detail=""):
        nonlocal checks
        checks += 1
        if condition:
            passed.append(name)
        else:
            message = name + ((" :: " + str(detail)) if detail else "")
            failures.append(message)
            print("  FAIL " + message)

    def eq(name, actual, expected):
        check(name, actual == expected,
              "expected %r, got %r" % (expected, actual))

    server = None
    try:
        root = temp_dir
        if ha_url:
            run_real_ha(bridge_source, root, ha_url, ha_token, check, eq)
        else:
            server = start_fake_ha(FAKE_HA_TOKEN)
            base = "http://127.0.0.1:%d" % server.server_address[1]
            build_runtime(root, bridge_source, base, FAKE_HA_TOKEN)
            run_fake_ha(root, base, server.store, check, eq)
    except Exception:
        traceback.print_exc()
        check("the suite ran to completion without an unexpected exception",
              False, "see the traceback above")
    finally:
        os.chdir(previous_cwd)
        if server is not None:
            server.shutdown()
            server.server_close()
        sys.modules.pop("patched_bridge_http", None)
        sys.modules.pop("patched_bridge_http_restart", None)
        logging.shutdown()
        shutil.rmtree(temp_dir, ignore_errors=True)

    for line in passed:
        print("  ok  " + line)
    print()
    if failures:
        print(f"schedule http integration tests FAILED "
              f"({checks} checks, {len(failures)} failed)")
        return 1
    print(f"schedule http integration tests passed ({checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
