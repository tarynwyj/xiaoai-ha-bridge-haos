#!/usr/bin/env python3
"""HAOS Ingress contract tests for the xiaoai_bridge add-on.

The add-on is served through Home Assistant Ingress (sidebar panel) *and* keeps a
direct port that now requires an access token.  These tests pin the contract that
makes that safe, in five groups:

1. port contract            config.yaml `ingress_port` / `ports` / `webui` agree,
                            `WEB_PORT` agrees, and `uvicorn.run(port=WEB_PORT,
                            proxy_headers=False)` is what actually serves;
2. frontend ingress safety  no root-absolute asset references, local CSS/JS is
                            inline, and the real `window.fetch` wrapper extracted
                            from the built HTML is executed in Node (rewrites
                            `/api/...` to `./api/...`, adds the CSRF header only to
                            state-changing calls, and resolves under the ingress
                            prefix instead of Home Assistant's own `/api/`);
3. bypass resistance        every `/api/*` route except `POST /api/session` is 401
                            without credentials, a forged `X-Ingress-Path` (and
                            `X-Remote-User-*` / `X-Forwarded-For`) from any
                            non-Supervisor peer is 401, only peer 172.30.32.2 is
                            trusted, and the middleware exempts nothing else;
4. token non-leakage        `GET /api/auth/token` answers 401 unauthenticated,
                            403 for a direct session/bearer and 200 only through
                            Ingress, and the token never appears in the login page,
                            error bodies, `/api/config` or the startup log;
5. legacy /data load        a 1.0.9-shaped `/data` tree loads unchanged, restores
                            identity + micoapi and keeps every key through
                            `GET /api/config`.

Usage (all paths optional except --bridge; defaults follow the image layout):

    python xiaoai_bridge/test_ingress_contract.py \
        --bridge build/bridge.py --html build/web/index.html \
        --config xiaoai_bridge/config.yaml

    # image build: only bridge.py + web/index.html exist, no node
    python xiaoai_bridge/test_ingress_contract.py \
        --bridge /app/bridge.py --html /app/web/index.html --no-node

Checks that cannot run (missing config.yaml, missing built HTML, no node) are
reported as ``SKIPPED <check>: <reason>`` and counted -- never silently passed.
(``--no-node`` exists because some shells cannot pass an empty ``--node ''``.)
Exit codes: 0 = nothing failed, 1 = at least one failure, 2 = usage / IO error.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

# ───────────────────────────── fixtures ─────────────────────────────
# Every credential below is synthetic and exists only in a throw-away /data tree.
FAKE_USER = "ingress-test-user"
FAKE_PASSWORD = "ingress-test-password"
FAKE_COOKIE = (
    "userId=ingress-test-user; passToken=ingress-test-pass; "
    "serviceToken=ingress-test-service; deviceId=ingress-test-device"
)
FAKE_HA_TOKEN = "ingress-test-ha-token"
FAKE_OPENAI_KEY = "sk-ingress-test-key"
FAKE_SSCURITY = "ingress-test-ssecurity"
FAKE_MICOAPI = ["ingress-test-ssecurity", "ingress-test-service"]

CURRENT_CONFIG = {
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

# A 1.0.9 install's /data/config/config.yaml: exactly the keys produced by the
# pinned upstream (e4c82d4b) ``_default_config()`` -- no 1.0.10-only keys such as
# the transient ``clear_login`` request flag, and no ``.access_token`` file yet.
LEGACY_109_CONFIG = {
    "xiaomi": {
        "username": "legacy-user",
        "password": "legacy-password",
        "cookie_text": "userId=legacy-user; passToken=legacy-pass; deviceId=legacy-device",
        "hardware": "L06A",
        "device_did": "legacy-speaker",
        "tts_reply": True,
    },
    "homeassistant": {
        "url": "http://homeassistant.local:8123",
        "token": "legacy-ha-token",
    },
    "openai": {
        "api_base": "https://api.openai.com",
        "api_key": "sk-legacy-key",
        "model": "gpt-4o-mini",
    },
    "ai_mode": False,
    "device_aliases": {"台灯": "switch.desk"},
    "poll_interval_seconds": 2,
    "intent_rules": [],
}

MASKED = "\u2022" * 8
INGRESS_HEADER = {"X-Ingress-Path": "/api/hassio_ingress/ingress-test-token"}
UI_HEADER = {"X-Requested-With": "xiaoai-bridge"}
SUPERVISOR = "172.30.32.2"
SUPERVISOR_PEER = (SUPERVISOR, 41234)
DEFAULT_PEER = ("testclient", 50000)
# Peers that must never inherit the Supervisor's Ingress trust.
NON_SUPERVISOR_PEERS = [
    ("172.30.32.3", 41234),
    ("172.30.33.7", 41234),
    ("192.168.1.10", 41234),
    ("testclient", 50000),
]

class SkipCheck(Exception):
    """The check cannot run in this context (reported as SKIPPED, never as ok)."""


# ───────────────────────────── reporting ─────────────────────────────
class Report:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.passed = 0
        self.skipped = 0
        self.failed = 0
        self.failures = []

    def check(self, name, function, *args, **kwargs):
        """Run one check; a SkipCheck is counted, any other error is a failure."""
        try:
            function(*args, **kwargs)
        except SkipCheck as error:
            print(f"SKIPPED {name}: {error}")
            self.skipped += 1
        except Exception as error:  # noqa: BLE001 - a test harness reports, never crashes
            self.failed += 1
            detail = f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
            print(f"FAIL {name}: {detail}")
            if self.verbose:
                import traceback

                traceback.print_exc()
            self.failures.append(name)
        else:
            print(f"  ok  {name}")
            self.passed += 1

    def summary(self) -> int:
        print(
            f"ingress contract: {self.passed} passed, {self.skipped} skipped, "
            f"{self.failed} failed"
        )
        if self.failures:
            print("failed checks:")
            for name in self.failures:
                print(f"  - {name}")
            return 1
        return 0


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
        wanted = name.lower()
        for key, value in self.headers:
            if key.decode("latin-1").lower() == wanted:
                return value.decode("latin-1")
        return None


class ASGIClient:
    """Drive the FastAPI app in-process; no network, no starlette TestClient.

    ``workdir`` is the throw-away /data tree: the bridge resolves config/, logs/
    and web/ relative to the process working directory, so every call switches
    into the tree and back (otherwise a request would read the caller's repo).
    """

    def __init__(self, app, workdir=None):
        self.app = app
        self.workdir = Path(workdir) if workdir is not None else None
        self.cookies = {}

    def request(self, method, path, *, headers=None, json_body=None,
                cookies=None, peer=DEFAULT_PEER, body=b""):
        previous = None
        if self.workdir is not None:
            previous = os.getcwd()
            os.chdir(self.workdir)
        try:
            return self._request(
                method, path, headers=headers, json_body=json_body, cookies=cookies,
                peer=peer, body=body,
            )
        finally:
            if previous is not None:
                os.chdir(previous)

    def _request(self, method, path, *, headers, json_body, cookies, peer, body):
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
            # Real browsers always send Host; the CSRF check compares Origin to it.
            header_pairs.append((b"host", b"testserver"))
        cookie_jar = dict(self.cookies)
        if cookies:
            cookie_jar.update(cookies)
        if cookie_jar:
            joined = "; ".join(f"{key}={value}" for key, value in cookie_jar.items())
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
            "client": peer,
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

        asyncio.run(self.app(scope, receive, send))
        response = Response(
            received["status"], received["headers"], bytes(received["body"])
        )
        set_cookie = response.header("set-cookie")
        if set_cookie:
            pair = set_cookie.split(";", 1)[0].strip()
            if "=" in pair:
                name, value = pair.split("=", 1)
                self.cookies[name] = value
        return response

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)


# ───────────────────────────── static analysis ─────────────────────────────
def parse_bridge(path: Path) -> tuple[str, ast.Module]:
    if not path.is_file():
        raise OSError(f"patched bridge.py not found: {path}")
    source = path.read_text(encoding="utf-8")
    return source, ast.parse(source)


def module_constant(tree: ast.Module, name: str):
    """Value of a module level ``NAME = <literal>`` assignment, else None."""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        if node.targets[0].id != name:
            continue
        try:
            return ast.literal_eval(node.value)
        except ValueError:
            return None
    return None


def find_call(tree: ast.Module, dotted_name: str):
    """First ``dotted_name(...)`` call in the module."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == dotted_name:
            return node
    return None


def find_function(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _assigned_expression(tree: ast.Module, name: str):
    """Unparsed right hand side of ``name = ...`` (first assignment found)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.unparse(node.value)
    return None


def call_keywords(call: ast.Call) -> dict:
    keywords = {}
    for keyword in call.keywords:
        keywords[keyword.arg] = keyword.value
    return keywords


def log_calls_touching(tree: ast.Module, names: set) -> list:
    """Every ``log.<level>(...)`` call whose arguments mention one of ``names``."""
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != "log":
            continue
        args = list(node.args) + [keyword.value for keyword in node.keywords]
        for argument in args:
            for sub_node in ast.walk(argument):
                if isinstance(sub_node, ast.Name) and sub_node.id in names:
                    offenders.append(
                        f"line {node.lineno}: {ast.unparse(node)[:120]}"
                    )
                    break
    return offenders


# ───────────────────────────── HTML / JS helpers ─────────────────────────────
ROOT_ABSOLUTE_PATTERNS = [
    ("src attribute", re.compile(r"""\bsrc\s*=\s*["']\/(?!\/)""")),
    ("href attribute", re.compile(r"""\bhref\s*=\s*["']\/(?!\/)""")),
    ("action attribute", re.compile(r"""\baction\s*=\s*["']\/(?!\/)""")),
    ("@import", re.compile(r"""@import\s+url\(\s*["']?\/(?!\/)""")),
    ("CSS url()", re.compile(r"""url\(\s*["']?\/(?!\/)""")),
    ("<link href>", re.compile(r"""<link[^>]*\bhref\s*=\s*["']\/(?!\/)""", re.I)),
]

THIRD_PARTY_PREFIXES = ("http://", "https://", "//", "data:", "mailto:", "#")


def html_references(html: str) -> list:
    """All ``src`` / ``href`` attribute values plus CSS ``url()`` targets."""
    values = []
    for match in re.finditer(r"""\b(?:src|href|action)\s*=\s*["']([^"']*)["']""", html, re.I):
        values.append(match.group(1).strip())
    for match in re.finditer(r"""url\(\s*["']?([^"')]*)["']?\s*\)""", html, re.I):
        values.append(match.group(1).strip())
    return values


def extract_fetch_wrapper(html: str) -> str:
    """Pull the real ``window.fetch`` wrapper out of the built page."""
    marker = html.find("window.fetch=function")
    if marker == -1:
        raise AssertionError(
            "built HTML has no window.fetch wrapper (the ingress-safe fetch patch was lost)"
        )
    if html.count("window.fetch=function") != 1:
        raise AssertionError(
            f"built HTML installs the window.fetch wrapper "
            f"{html.count('window.fetch=function')} times; expected exactly one"
        )
    start = html.rfind("(function(){", 0, marker)
    if start == -1:
        raise AssertionError("could not find the start of the fetch wrapper IIFE")
    end = html.find("})();", marker)
    if end == -1:
        raise AssertionError("could not find the end of the fetch wrapper IIFE")
    return html[start:end + len("})();")]


NODE_HARNESS = r"""// Generated by test_ingress_contract.py -- runs the real page fetch wrapper.
import assert from 'node:assert/strict';
import fs from 'node:fs';

const wrapperPath = process.argv[2];
const mode = process.argv[3] || 'rewrites';
const wrapperSource = fs.readFileSync(wrapperPath, 'utf8');

const calls = [];
let docHtml = '';
let reloads = 0;

globalThis.__status = 200;
globalThis.window = {
  fetch(input, init) {
    calls.push({ input, init });
    return Promise.resolve({ status: globalThis.__status });
  },
};
globalThis.document = {
  documentElement: {
    get innerHTML() { return docHtml; },
    set innerHTML(value) { docHtml = String(value); },
  },
  getElementById() { return null; },
  querySelectorAll() { return []; },
};
globalThis.location = { reload() { reloads += 1; } };
globalThis.setTimeout = () => 0;
globalThis.localStorage = { getItem() { return null; }, setItem() {} };

new Function(wrapperSource)();

const last = () => calls[calls.length - 1];
const ok = (message) => console.log('PASS ' + message);

if (!globalThis.window.fetch || globalThis.window.fetch === globalThis.window.__native) {
  throw new Error('the wrapper did not replace window.fetch');
}

if (mode === 'rewrites') {
  await globalThis.window.fetch('/api/config');
  assert.equal(last().input, './api/config');
  assert.equal(last().init, undefined, 'a bare GET must not synthesise init');
  ok('fetch("/api/config") -> "./api/config"');

  await globalThis.window.fetch('/api/status');
  assert.equal(last().input, './api/status');
  ok('fetch("/api/status") -> "./api/status"');

  await globalThis.window.fetch('api/config');
  assert.equal(last().input, 'api/config', 'an already relative URL must be untouched');
  await globalThis.window.fetch('./api/config');
  assert.equal(last().input, './api/config', 'an already dotted URL must be untouched');
  ok('relative URLs are left untouched');

  const originalHeaders = { 'Content-Type': 'application/json' };
  const postInit = { method: 'POST', headers: originalHeaders };
  await globalThis.window.fetch('/api/config', postInit);
  assert.equal(last().init.headers['X-Requested-With'], 'xiaoai-bridge');
  assert.equal(originalHeaders['X-Requested-With'], undefined,
    'the caller header object must not be mutated in place');
  ok('POST carries X-Requested-With: xiaoai-bridge');

  await globalThis.window.fetch('/api/config', { method: 'delete' });
  assert.equal(last().init.headers['X-Requested-With'], 'xiaoai-bridge',
    'lowercase methods are state changing too');
  ok('non-GET without headers still gets the CSRF header');

  const headersLike = { set(name, value) { this[name] = value; } };
  await globalThis.window.fetch('/api/config', { method: 'PATCH', headers: headersLike });
  assert.equal(headersLike['X-Requested-With'], 'xiaoai-bridge',
    'a Headers-like object must be updated through set()');
  ok('Headers-like objects are updated through set()');

  const before = calls.length;
  await globalThis.window.fetch('/api/status', { method: 'GET' });
  assert.equal(last().init.headers, undefined, 'GET must not carry the CSRF header');
  assert.equal(calls.length, before + 1);
  ok('GET does not carry X-Requested-With');

  globalThis.__status = 401;
  const expired = await globalThis.window.fetch('/api/status');
  assert.equal(expired.status, 401);
  assert.ok(docHtml.includes('\u4f1a\u8bdd\u5df2\u5931\u6548'),
    'a 401 must swap in the "session expired" notice');
  assert.equal(reloads, 0, 'the reload is scheduled through setTimeout, not called directly');
  ok('401 response is handed back and triggers the login redirect notice');
}

if (mode === 'ingress-url') {
  await globalThis.window.fetch('/api/config');
  const rewritten = last().input;
  const base = 'https://ha.local:8123/api/hassio_ingress/TOKEN/';
  const resolved = new URL(rewritten, base).href;
  assert.equal(resolved,
    'https://ha.local:8123/api/hassio_ingress/TOKEN/api/config',
    'the rewritten URL must stay inside the ingress prefix');
  const naive = new URL('/api/config', base).href;
  assert.equal(naive, 'https://ha.local:8123/api/config',
    'sanity: a root-absolute path really does escape to Home Assistant itself');
  assert.notEqual(resolved, naive);
  ok('rewritten request resolves under /api/hassio_ingress/TOKEN/, not on HA /api/config');

  const deep = new URL(rewritten, base + 'panel/').href;
  assert.equal(deep, 'https://ha.local:8123/api/hassio_ingress/TOKEN/panel/api/config',
    'a page served below the prefix must still resolve relative to its own directory');
  ok('nested ingress pages resolve inside their own directory');
}

if (mode === 'direct-url') {
  await globalThis.window.fetch('/api/config');
  const rewritten = last().input;
  assert.equal(new URL(rewritten, 'http://host:47521/').href,
    'http://host:47521/api/config', 'direct port base with trailing slash');
  assert.equal(new URL(rewritten, 'http://host:47521').href,
    'http://host:47521/api/config', 'direct port base without trailing slash');
  ok('rewritten request resolves to http://host:47521/api/config');
}
"""


# ───────────────────────────── runtime harness ─────────────────────────────
class BridgeRuntime:
    """One isolated /data-like tree with the patched bridge imported into it."""

    def __init__(self, root: Path, bridge_source: Path, html_source: Path | None,
                 config: dict, module_name: str):
        self.root = root
        self.bridge_source = bridge_source
        self.html_source = html_source
        self.config = config
        self.module_name = module_name
        self.module = None
        self.snapshot = {}
        self._closed = False

    # -- lifecycle -----------------------------------------------------
    def build(self):
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "logs").mkdir(parents=True, exist_ok=True)
        (self.root / "web").mkdir(parents=True, exist_ok=True)
        shutil.copy(self.bridge_source, self.root / "bridge.py")
        if self.html_source is not None:
            shutil.copy(self.html_source, self.root / "web" / "index.html")
        config_path = self.root / "config" / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(self.config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        (self.root / "config" / ".mi.token").write_text(
            json.dumps(
                {
                    "userId": FAKE_USER,
                    "passToken": "ingress-test-pass",
                    "deviceId": "ingress-test-device",
                    "micoapi": list(FAKE_MICOAPI),
                }
            ),
            encoding="utf-8",
        )
        (self.root / "config" / "auth.json").write_text(
            json.dumps(
                {
                    "userId": FAKE_USER,
                    "passToken": "ingress-test-pass",
                    "deviceId": "ingress-test-device",
                }
            ),
            encoding="utf-8",
        )
        (self.root / "config" / "last_timestamp.json").write_text(
            json.dumps({"speaker-1": 1_700_000_000_000}), encoding="utf-8"
        )
        self.snapshot = {
            path.name: path.read_bytes()
            for path in (self.root / "config").iterdir()
            if path.is_file()
        }
        previous = os.getcwd()
        os.chdir(self.root)
        try:
            spec = importlib.util.spec_from_file_location(
                self.module_name, self.root / "bridge.py"
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[self.module_name] = module
            spec.loader.exec_module(module)
        finally:
            os.chdir(previous)
        self.module = module
        return module

    def activate(self):
        """Context manager: run with ``root`` as the process working directory."""
        return _ActivatedRuntime(self)

    def close(self):
        if self._closed:
            return
        self._closed = True
        # The FileHandler keeps the log file open on Windows: close it before the
        # tree is removed, otherwise rmtree fails and hides the real results.
        root_logger = logging.getLogger()
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001 - closing a handler must never fail the run
                pass
        shutil.rmtree(self.root, ignore_errors=True)


class _ActivatedRuntime:
    def __init__(self, runtime: BridgeRuntime):
        self.runtime = runtime
        self.previous = None

    def __enter__(self):
        self.previous = os.getcwd()
        os.chdir(self.runtime.root)
        return self.runtime.module

    def __exit__(self, *exc_info):
        os.chdir(self.previous)
        return False


class Context:
    def __init__(self, args):
        self.args = args
        self.bridge_path = Path(args.bridge).resolve()
        self.html_path = Path(args.html).resolve() if args.html else None
        self.config_path = None
        self.workdir = Path(tempfile.mkdtemp(prefix="ingress-contract-"))
        self._tree = None
        self._source = None
        self._config = None
        self._wrapper = None
        self.current = None
        self.legacy = None
        self.client = None
        self._bodies = []

    # -- inputs --------------------------------------------------------
    def source(self) -> str:
        if self._source is None:
            self._source, self._tree = parse_bridge(self.bridge_path)
        return self._source

    def tree(self) -> ast.Module:
        if self._tree is None:
            self.source()
        return self._tree

    def config(self) -> dict:
        if self._config is not None:
            return self._config
        candidates = []
        if self.args.config:
            candidates.append(Path(self.args.config))
        if self.args.repo_root:
            root = Path(self.args.repo_root)
            candidates.extend(
                [root / "xiaoai_bridge" / "config.yaml", root / "config.yaml"]
            )
        for candidate in candidates:
            if candidate.is_file():
                self.config_path = candidate.resolve()
                self._config = yaml.safe_load(
                    candidate.read_text(encoding="utf-8")
                ) or {}
                return self._config
        raise SkipCheck(
            "config.yaml not supplied (image build has none; pass --config or --repo-root)"
        )

    def html(self) -> str:
        if self.html_path is None:
            raise SkipCheck(
                "built web/index.html not supplied (pass --html to run frontend checks)"
            )
        if not self.html_path.is_file():
            raise SkipCheck(f"built web/index.html not found: {self.html_path}")
        return self.html_path.read_text(encoding="utf-8")

    def fetch_wrapper(self) -> str:
        if self._wrapper is None:
            self._wrapper = extract_fetch_wrapper(self.html())
        return self._wrapper

    # -- runtime -------------------------------------------------------
    def build_current(self) -> BridgeRuntime:
        if self.current is not None:
            return self.current
        root = Path(tempfile.mkdtemp(prefix="ingress-current-"))
        html_source = self.html_path if self.html_path and self.html_path.is_file() else None
        runtime = BridgeRuntime(
            root, self.bridge_path, html_source, CURRENT_CONFIG, "ingress_bridge_current"
        )
        runtime.build()
        self.current = runtime
        self.client = ASGIClient(runtime.module.app, workdir=runtime.root)
        return runtime

    def close_current(self):
        if self.current is not None:
            self.current.close()
            self.current = None
            self.client = None

    def build_legacy(self) -> BridgeRuntime:
        if self.legacy is not None:
            return self.legacy
        root = Path(tempfile.mkdtemp(prefix="ingress-legacy-"))
        html_source = self.html_path if self.html_path and self.html_path.is_file() else None
        runtime = BridgeRuntime(
            root, self.bridge_path, html_source, LEGACY_109_CONFIG, "ingress_bridge_legacy"
        )
        # 1.0.9 kept a micoapi token + a last_timestamp.json under config/.
        runtime.build()
        self.legacy = runtime
        return runtime

    def close_all(self):
        self.close_current()
        if self.legacy is not None:
            self.legacy.close()
            self.legacy = None
        shutil.rmtree(self.workdir, ignore_errors=True)
        try:
            logging.shutdown()
        except Exception:  # noqa: BLE001 - shutdown of already closed handlers
            pass

    def record(self, label, response, allow_secret=False):
        """Remember a response so the token-leak check can scan every body."""
        self._bodies.append((label, response.text, allow_secret))
        return response

    def recorded_bodies(self):
        return [
            (label, text)
            for label, text, allow_secret in self._bodies
            if not allow_secret
        ]


def require_current(ctx: Context) -> BridgeRuntime:
    if ctx.current is None:
        raise SkipCheck("bridge runtime was not bootstrapped (see the runtime check)")
    return ctx.current


def require_legacy(ctx: Context) -> BridgeRuntime:
    if ctx.legacy is None:
        raise SkipCheck("legacy bridge runtime was not bootstrapped")
    return ctx.legacy


# ───────────────────────── group 1: port contract ─────────────────────────
def check_config_port_contract(ctx: Context):
    config = ctx.config()
    ingress_port = config.get("ingress_port")
    assert isinstance(ingress_port, int) and not isinstance(ingress_port, bool), (
        f"config.yaml ingress_port must be an integer, got {ingress_port!r}"
    )
    ports = config.get("ports")
    assert isinstance(ports, dict) and ports, (
        f"config.yaml must keep the direct ports mapping, got {ports!r}"
    )
    key = f"{ingress_port}/tcp"
    assert key in ports, (
        f"ports must contain {key!r} for ingress_port={ingress_port}, got {sorted(ports)}"
    )
    host_port = ports[key]
    assert int(host_port) == ingress_port, (
        f"ports[{key!r}] must be {ingress_port}, got {host_port!r}"
    )
    webui = str(config.get("webui") or "")
    match = re.search(r"\[PORT:(\d+)\]", webui)
    assert match, f"webui must expose a [PORT:n] placeholder, got {webui!r}"
    assert int(match.group(1)) == ingress_port, (
        f"webui [PORT:{match.group(1)}] must equal ingress_port={ingress_port}"
    )


def check_config_ingress_flags(ctx: Context):
    config = ctx.config()
    assert config.get("ingress") is True, (
        f"config.yaml must set ingress: true, got {config.get('ingress')!r}"
    )
    assert config.get("panel_admin") is True, (
        f"config.yaml must set panel_admin: true, got {config.get('panel_admin')!r}"
    )


def check_bridge_port_constant(ctx: Context):
    tree = ctx.tree()
    value = module_constant(tree, "WEB_PORT")
    assert isinstance(value, int) and not isinstance(value, bool), (
        f"patched bridge.py must define WEB_PORT as an integer literal, got {value!r}"
    )
    assert value > 0


def check_bridge_port_matches_config(ctx: Context):
    config = ctx.config()
    ingress_port = config.get("ingress_port")
    value = module_constant(ctx.tree(), "WEB_PORT")
    assert value == ingress_port, (
        f"bridge WEB_PORT={value!r} must equal config.yaml ingress_port={ingress_port!r}"
    )


def check_uvicorn_call(ctx: Context):
    call = find_call(ctx.tree(), "uvicorn.run")
    assert call is not None, "patched bridge.py must start uvicorn with uvicorn.run(...)"
    keywords = call_keywords(call)
    assert "port" in keywords, (
        f"uvicorn.run must pass port=..., got keywords {sorted(keywords)}"
    )
    port_source = ast.unparse(keywords["port"])
    assert port_source == "WEB_PORT", (
        f"uvicorn.run must pass port=WEB_PORT, got port={port_source}"
    )
    assert "proxy_headers" in keywords, (
        "uvicorn.run must pass proxy_headers=False explicitly (Ingress trust uses the "
        "real TCP peer, never X-Forwarded-For)"
    )
    assert ast.literal_eval(keywords["proxy_headers"]) is False, (
        f"uvicorn.run must pass proxy_headers=False, got {ast.unparse(keywords['proxy_headers'])}"
    )
    host = keywords.get("host")
    assert host is not None and ast.unparse(host) == "'0.0.0.0'", (
        f"uvicorn.run must listen on 0.0.0.0 for Ingress, got {ast.unparse(host) if host else None}"
    )


# ───────────────────────── group 2: frontend ingress safety ─────────────────────────
def check_html_root_absolute_refs(ctx: Context):
    html = ctx.html()
    problems = []
    for label, pattern in ROOT_ABSOLUTE_PATTERNS:
        for match in pattern.finditer(html):
            snippet = html[max(0, match.start() - 40):match.end() + 40].replace("\n", " ")
            problems.append(f"{label}: ...{snippet}...")
    assert not problems, (
        "built HTML still resolves asset references from the site root, which 404s "
        "below /api/hassio_ingress/<token>/:\n  " + "\n  ".join(problems[:5])
    )


def check_html_local_assets_inline(ctx: Context):
    html = ctx.html()
    external_local = []
    third_party = []
    for tag in re.findall(r"<link\b[^>]*>", html, re.I) + re.findall(
        r"<script\b[^>]*>", html, re.I
    ):
        match = re.search(r"""\b(?:src|href)\s*=\s*["']([^"']*)["']""", tag, re.I)
        if not match:
            continue  # inline style / script, nothing to fetch
        target = match.group(1).strip()
        if target.startswith(THIRD_PARTY_PREFIXES):
            third_party.append(target)
            continue
        external_local.append(f"{tag} -> {target}")
    for target in html_references(html):
        if target.startswith("/"):
            external_local.append(f"root-absolute asset reference -> {target}")
    assert not external_local, (
        "the page must not fetch add-on local CSS/JS assets by path (they 404 behind "
        "the ingress prefix); inline them or use a relative path:\n  "
        + "\n  ".join(external_local[:5])
    )
    assert re.search(r"<style\b", html, re.I), "the built page must keep its CSS inline"
    assert re.search(r"<script\b(?![^>]*\bsrc=)", html, re.I), (
        "the built page must keep its JavaScript inline"
    )
    if third_party:
        # Absolute third-party URLs bypass the ingress path entirely: no 404 risk,
        # but the page depends on an external host (fonts degrade gracefully).
        print(
            f"       note: {len(third_party)} third-party absolute resource(s) are not "
            f"resolved through Ingress: {', '.join(sorted(set(third_party))[:3])}"
        )


def _run_node(ctx: Context, mode: str) -> str:
    node = ctx.args.node
    if node == "":
        raise SkipCheck("node disabled with --node '' (image build has no node)")
    if node:
        if not Path(node).is_file():
            raise SkipCheck(f"node executable not found at {node}")
    else:
        node = shutil.which("node")
        if not node:
            raise SkipCheck("node executable not found on PATH (pass --node <path>)")
    wrapper_path = ctx.workdir / "fetch-wrapper.js"
    wrapper_path.write_text(ctx.fetch_wrapper(), encoding="utf-8")
    harness_path = ctx.workdir / "harness.mjs"
    harness_path.write_text(NODE_HARNESS, encoding="utf-8")
    completed = subprocess.run(
        [node, str(harness_path), str(wrapper_path), mode],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"node harness [{mode}] exited {completed.returncode}\n"
            f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
        )
    return completed.stdout


def check_fetch_rewrites(ctx: Context):
    _run_node(ctx, "rewrites")


def check_fetch_resolves_under_ingress(ctx: Context):
    _run_node(ctx, "ingress-url")


def check_fetch_resolves_on_direct_port(ctx: Context):
    _run_node(ctx, "direct-url")


# ───────────────────────── group 3/4 runtime helpers ─────────────────────────
def check_runtime_bootstrap(ctx: Context):
    runtime = ctx.build_current()
    module = runtime.module
    token_path = runtime.root / "config" / ".access_token"
    assert token_path.is_file(), (
        "the bridge must create config/.access_token on first start"
    )
    token = getattr(module, "ACCESS_TOKEN", None)
    assert isinstance(token, str) and len(token) >= 32, (
        f"ACCESS_TOKEN must be a >=256-bit random string, got length {len(token or '')}"
    )
    assert token_path.read_text(encoding="utf-8").strip() == token, (
        "the persisted token file must hold the in-memory token"
    )
    assert getattr(module, "app", None) is not None, "the bridge must expose a FastAPI app"


def check_all_api_routes_require_auth(ctx: Context):
    runtime = require_current(ctx)
    module = runtime.module
    client = ctx.client
    routes = {}
    for route in module.app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None) or set()
        if path.startswith("/api/"):
            routes[path] = sorted(methods)
    assert routes, "no /api routes found on the app"
    exempt = {("/api/session", "POST")}
    probed = 0
    for path in sorted(routes):
        probe_path = re.sub(r"\{[^}]+\}", "ingress-probe-id", path)
        for method in routes[path]:
            if (path, method) in exempt:
                continue
            if method in ("GET", "HEAD"):
                response = client.get(probe_path)
            elif method == "DELETE":
                response = client.request("DELETE", probe_path)
            else:
                response = client.post(probe_path, json_body={})
            probed += 1
            assert response.status_code == 401, (
                f"{method} {path} must be 401 without credentials, "
                f"got {response.status_code}: {ctx_record_text(ctx, response)}"
            )
            body = response.json()
            assert body.get("auth_required") is True, (
                f"{method} {path} must answer with auth_required=true, got {body}"
            )
    assert probed >= 15, f"expected the management API to have many routes, probed {probed}"
    # The login endpoint itself must still reject a wrong token.
    wrong = client.post("/api/session", json_body={"token": "not-the-token"})
    assert wrong.status_code == 401, (
        f"POST /api/session must reject a wrong token, got {wrong.status_code}"
    )


def ctx_record_text(ctx: Context, response) -> str:
    """Response text with any token-like value masked (failure messages are logged)."""
    text = response.text
    runtime = ctx.current
    token = getattr(runtime.module, "ACCESS_TOKEN", None) if runtime else None
    if token:
        text = text.replace(token, "<ACCESS_TOKEN>")
    return text[:160]


def check_middleware_exempts_only_session(ctx: Context):
    tree = ctx.tree()
    middleware = find_function(tree, "_enforce_access_control")
    assert middleware is not None, (
        "patched bridge.py must keep the _enforce_access_control middleware"
    )
    literals = {
        node.value
        for node in ast.walk(middleware)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("/api")
    }
    assert literals == {"/api/session", "/api/"}, (
        "the middleware must only special-case POST /api/session; it mentions "
        f"{sorted(literals)}"
    )
    first_if = next(
        (node for node in ast.walk(middleware) if isinstance(node, ast.If)), None
    )
    assert first_if is not None, "the middleware must start with its exemption guard"
    guard = ast.unparse(first_if.test)
    assert "/api/session" in guard, (
        f"the first middleware guard must be the login exemption, got: {guard}"
    )
    assert "POST" in guard and "method" in guard, (
        "the /api/session exemption must be limited to POST, got: " + guard
    )
    # No route may declare itself exempt from authentication.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                source = ast.unparse(decorator)
                if "auth" in source.lower() and "exempt" in source.lower():
                    raise AssertionError(
                        f"unexpected route level auth bypass on {node.name}: {source}"
                    )


def check_forged_ingress_header_rejected(ctx: Context):
    require_current(ctx)
    client = ctx.client
    forged = [
        ("no extra headers", dict(INGRESS_HEADER)),
        (
            "with HA proxy user headers",
            {
                **INGRESS_HEADER,
                "X-Remote-User-Id": "home-assistant-user",
                "X-Remote-User-Name": "admin",
                "X-Remote-User-Display-Name": "Admin",
            },
        ),
        (
            "with supervisor looking forwarded-for",
            {**INGRESS_HEADER, "X-Forwarded-For": SUPERVISOR, "X-Real-IP": SUPERVISOR},
        ),
    ]
    for label, headers in forged:
        response = ctx.record(
            f"forged ingress ({label})",
            client.get("/api/status", headers=headers),
        )
        assert response.status_code == 401, (
            f"a forged X-Ingress-Path from a non-Supervisor peer must be 401 ({label}), "
            f"got {response.status_code}"
        )
    for peer in NON_SUPERVISOR_PEERS:
        response = ctx.record(
            f"ingress header from {peer[0]}",
            client.get("/api/status", headers=INGRESS_HEADER, peer=peer),
        )
        assert response.status_code == 401, (
            f"peer {peer[0]} must not be trusted just because it sends X-Ingress-Path, "
            f"got {response.status_code}"
        )


def check_supervisor_peer_authorised(ctx: Context):
    require_current(ctx)
    client = ctx.client
    status = ctx.record(
        "supervisor ingress /api/status",
        client.get("/api/status", headers=INGRESS_HEADER, peer=SUPERVISOR_PEER),
    )
    assert status.status_code == 200, (
        f"the Supervisor peer with X-Ingress-Path must be authorised, "
        f"got {status.status_code}: {ctx_record_text(ctx, status)}"
    )
    body = status.json()
    assert "auth_required" not in body, (
        f"the authorised response must be the real payload, got {body}"
    )
    assert "running" in body, f"/api/status payload looks wrong: {body}"
    if ctx.html_path is not None and ctx.html_path.is_file():
        root_page = ctx.record(
            "supervisor ingress /",
            client.get("/", headers=INGRESS_HEADER, peer=SUPERVISOR_PEER),
        )
        assert root_page.status_code == 200
        assert 'id="page-rules"' in root_page.text, (
            "ingress must serve the management UI, not the direct-access login page"
        )


def check_peer_is_supervisor_strict(ctx: Context):
    module = require_current(ctx).module
    trusted = [SUPERVISOR]
    untrusted = [
        "172.30.32.3",
        "172.30.32.20",
        "172.30.33.7",
        "172.30.32.1",
        "192.168.1.10",
        "127.0.0.1",
        "::1",
        "::ffff:172.30.32.2",
        "testclient",
        "",
        None,
    ]
    for host in trusted:
        assert module._peer_is_supervisor(host) is True, f"{host} must be trusted"
    for host in untrusted:
        assert module._peer_is_supervisor(host) is False, (
            f"{host!r} must not be trusted as the Supervisor"
        )


def check_docs_disabled(ctx: Context):
    require_current(ctx)
    for path in ("/openapi.json", "/docs", "/redoc"):
        response = ctx.record(f"docs probe {path}", ctx.client.get(path))
        assert response.status_code == 404, (
            f"{path} must stay disabled on the add-on, got {response.status_code}"
        )


def check_auth_token_requires_ingress(ctx: Context):
    runtime = require_current(ctx)
    client = ctx.client
    token = runtime.module.ACCESS_TOKEN

    anonymous = ctx.record("auth/token anonymous", client.get("/api/auth/token"))
    assert anonymous.status_code == 401, (
        f"/api/auth/token must be 401 unauthenticated, got {anonymous.status_code}"
    )

    session = ctx.client.post("/api/session", json_body={"token": token})
    assert session.status_code == 200, (
        f"direct login with the real token must succeed, got {session.status_code}"
    )
    cookie_name = runtime.module.SESSION_COOKIE
    cookie_value = ctx.client.cookies.get(cookie_name)
    assert cookie_value, "login must set the session cookie"
    assert token not in cookie_value, "the session cookie must not contain the token"
    guarded = ctx.record(
        "auth/token with session cookie",
        ASGIClient(runtime.module.app, workdir=runtime.root).get(
            "/api/auth/token", cookies={cookie_name: cookie_value}
        ),
    )
    assert guarded.status_code == 403, (
        f"a direct session must not reveal the token, got {guarded.status_code}"
    )

    bearer = ctx.record(
        "auth/token with bearer",
        client.get("/api/auth/token", headers={"Authorization": f"Bearer {token}"}),
    )
    assert bearer.status_code == 403, (
        f"a direct bearer token must not reveal the direct token, got {bearer.status_code}"
    )
    admin_header = ctx.record(
        "auth/token with X-Admin-Token",
        client.get("/api/auth/token", headers={"X-Admin-Token": token}),
    )
    assert admin_header.status_code == 403, (
        f"X-Admin-Token must not reveal the direct token, got {admin_header.status_code}"
    )

    revealed = ctx.record(
        "auth/token from supervisor",
        client.get("/api/auth/token", headers=INGRESS_HEADER, peer=SUPERVISOR_PEER),
        allow_secret=True,  # this one response is *supposed* to carry the token
    )
    assert revealed.status_code == 200, (
        f"the ingress panel must be able to read the token, got {revealed.status_code}"
    )
    body = revealed.json()
    assert body.get("token") == token, "the endpoint must return the real direct token"
    assert body.get("ok") is True
    assert str(body.get("path", "")).endswith(".access_token"), (
        f"the endpoint should name the token file, got {body.get('path')!r}"
    )


def check_token_not_leaked(ctx: Context):
    runtime = require_current(ctx)
    token = runtime.module.ACCESS_TOKEN
    leaks = []
    for label, text in ctx.recorded_bodies():
        if token in text:
            leaks.append(f"response body: {label}")
    login_page = ctx.client.get("/")
    assert login_page.status_code == 200
    if token in login_page.text:
        leaks.append("unauthenticated GET / (login page)")
    config = ctx.client.get("/api/config", headers=INGRESS_HEADER, peer=SUPERVISOR_PEER)
    assert config.status_code == 200, (
        f"GET /api/config must work for the ingress panel, got {config.status_code}"
    )
    if token in config.text:
        leaks.append("GET /api/config")
    serialised = json.dumps(config.json(), ensure_ascii=False)
    for secret in (FAKE_PASSWORD, "ingress-test-pass", "ingress-test-service",
                   FAKE_HA_TOKEN, FAKE_OPENAI_KEY):
        if secret in serialised:
            leaks.append(f"/api/config leaked the credential {secret!r}")
    assert not leaks, "the direct access token/credentials leaked: " + "; ".join(leaks)


def check_token_absent_from_logs(ctx: Context):
    runtime = require_current(ctx)
    token = runtime.module.ACCESS_TOKEN
    records = list(getattr(runtime.module, "log_records", []))
    log_file = runtime.root / "logs" / "bridge.log"
    file_text = log_file.read_text(encoding="utf-8") if log_file.is_file() else ""
    assert records, "the bridge produced no in-memory log records at startup"
    combined = "\n".join(record.get("msg", "") for record in records)
    leaks = []
    if token in combined:
        leaks.append("in-memory log_records")
    if token in file_text:
        leaks.append(f"log file {log_file.name}")
    assert not leaks, "the access token must never be logged, but it appeared " + ", ".join(leaks)
    assert ".access_token" in combined or "access_token" in combined, (
        "the startup log should name the token file (path only) so users can find it"
    )


def check_no_token_log_call(ctx: Context):
    offenders = log_calls_touching(ctx.tree(), {"ACCESS_TOKEN", "SESSION_VALUE"})
    assert not offenders, (
        "no log call may interpolate the direct access token:\n  " + "\n  ".join(offenders)
    )


# ───────────────────────── group 5: legacy /data ─────────────────────────
def check_legacy_config_loads_unchanged(ctx: Context):
    runtime = ctx.build_legacy()
    config_path = runtime.root / "config" / "config.yaml"
    before = config_path.read_bytes()
    expected = yaml.safe_load(before.decode("utf-8"))
    with runtime.activate() as module:
        loaded = module.load_config()
    assert loaded == expected, (
        "load_config() must return the 1.0.9 file unchanged, "
        f"difference: {_first_difference(expected, loaded)}"
    )
    assert config_path.read_bytes() == before, (
        "loading a legacy config must not rewrite config.yaml"
    )
    with runtime.activate() as module:
        defaults = module._default_config()
    # A 1.0.9 install may carry keys the current defaults no longer seed (cookie_text
    # is written lazily, device_aliases holds live values), so only the sections are
    # compared: 1.0.10 must still know every top level section of a 1.0.9 file.
    dropped_sections = sorted(set(expected) - set(defaults))
    assert not dropped_sections, (
        "the 1.0.10 defaults dropped config sections a 1.0.9 install still has: "
        + ", ".join(dropped_sections)
    )
    assert "clear_login" not in expected.get("xiaomi", {}), (
        "a 1.0.9 config.yaml cannot contain the transient 1.0.10 clear_login flag"
    )
    records = "\n".join(
        record.get("msg", "") for record in getattr(runtime.module, "log_records", [])
    )
    assert ".access_token" in records, (
        "importing a legacy /data tree must start up normally (token bootstrapped)"
    )
    # 1.0.10 must keep using the 1.0.9 timestamp file instead of silently moving it.
    timestamp_path = _assigned_expression(ctx.tree(), "LAST_TS_PATH")
    assert timestamp_path == "CONFIG_PATH.parent / 'last_timestamp.json'", (
        "bridge.py must keep reading config/last_timestamp.json, got "
        f"{timestamp_path!r}"
    )
    writer_source = ctx.source()
    assert "LAST_TS_PATH.write_text" in writer_source, (
        "the poll loop must persist the legacy timestamp file (no silent re-processing)"
    )


def check_legacy_token_restore(ctx: Context):
    runtime = require_legacy(ctx)

    class Account:
        token = None

    account = Account()
    with runtime.activate() as module:
        module._set_token(account, module.load_config())
    restored = account.token
    assert isinstance(restored, dict) and restored, "_set_token must restore a token dict"
    assert restored.get("userId") == FAKE_USER, f"userId not restored: {restored}"
    assert restored.get("passToken") == "ingress-test-pass", (
        f"passToken not restored: {restored}"
    )
    assert restored.get("deviceId") == "ingress-test-device", (
        f"deviceId not restored: {restored}"
    )
    micoapi = restored.get("micoapi")
    assert micoapi is not None, (
        "_set_token must restore micoapi so a legacy install does not relogin"
    )
    assert list(micoapi) == FAKE_MICOAPI, f"micoapi not restored verbatim: {micoapi}"


def check_legacy_config_api_preserves_keys(ctx: Context):
    runtime = require_legacy(ctx)
    config_path = runtime.root / "config" / "config.yaml"
    before = config_path.read_bytes()
    expected = yaml.safe_load(before.decode("utf-8"))
    client = ASGIClient(runtime.module.app, workdir=runtime.root)
    response = client.get("/api/config", headers=INGRESS_HEADER, peer=SUPERVISOR_PEER)
    assert response.status_code == 200, (
        f"GET /api/config must serve a legacy install, got {response.status_code}"
    )
    body = response.json()
    missing = _missing_keys(expected, body)
    assert not missing, (
        "GET /api/config dropped legacy config keys: " + ", ".join(missing)
    )
    assert body["xiaomi"]["password"] == MASKED, "legacy password must come back masked"
    assert body["xiaomi"]["cookie_text"] == MASKED, "legacy cookie_text must come back masked"
    assert body["homeassistant"]["token"] == MASKED, "legacy HA token must come back masked"
    for secret in ("legacy-password", "legacy-pass", "legacy-ha-token", "sk-legacy-key"):
        assert secret not in response.text, f"GET /api/config leaked {secret!r}"
    assert config_path.read_bytes() == before, (
        "GET /api/config must not rewrite the legacy config file"
    )


def check_legacy_files_untouched(ctx: Context):
    runtime = require_legacy(ctx)
    names = ["config.yaml", "auth.json", ".mi.token", "last_timestamp.json"]
    missing = [name for name in names if name not in runtime.snapshot]
    assert not missing, f"legacy /data tree is missing {missing}"
    for name, content in runtime.snapshot.items():
        if name == ".access_token":
            continue
        current = (runtime.root / "config" / name).read_bytes()
        assert current == content, (
            f"{name} was rewritten while loading a legacy /data tree "
            f"({len(content)} -> {len(current)} bytes)"
        )


# ───────────────────────────── small helpers ─────────────────────────────
def _missing_keys(expected, actual, prefix: str = "") -> list:
    missing = []
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [prefix or "<root>"]
        for key, value in expected.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in actual:
                missing.append(child)
            else:
                missing.extend(_missing_keys(value, actual[key], child))
    return missing


def _first_difference(expected, actual, prefix: str = "") -> str:
    if type(expected) is not type(actual) and not (
        isinstance(expected, (int, float)) and isinstance(actual, (int, float))
    ):
        return f"{prefix or '<root>'}: {type(expected).__name__} != {type(actual).__name__}"
    if isinstance(expected, dict):
        for key in expected:
            if key not in actual:
                return f"{prefix}.{key} missing"
            difference = _first_difference(expected[key], actual[key], f"{prefix}.{key}")
            if difference:
                return difference
        extra = sorted(set(actual) - set(expected))
        return f"{prefix}: unexpected keys {extra}" if extra else ""
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{prefix}: list length {len(expected)} != {len(actual)}"
        for index, (left, right) in enumerate(zip(expected, actual)):
            difference = _first_difference(left, right, f"{prefix}[{index}]")
            if difference:
                return difference
        return ""
    if expected != actual:
        return f"{prefix}: {expected!r} != {actual!r}"
    return ""


# ───────────────────────────── entry point ─────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="HAOS Ingress contract tests for the xiaoai_bridge add-on.",
    )
    parser.add_argument(
        "--bridge",
        default="/app/bridge.py",
        help="patched bridge.py (default: /app/bridge.py)",
    )
    parser.add_argument(
        "--html",
        default=None,
        help="built web/index.html; frontend checks are SKIPPED without it",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="add-on config.yaml; port/flags checks are SKIPPED without it",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="add-on repo root, used to find xiaoai_bridge/config.yaml when "
        "--config is omitted",
    )
    parser.add_argument(
        "--node",
        default=None,
        help="node executable for the frontend checks (default: node from PATH; "
        "pass '' to skip them)",
    )
    parser.add_argument(
        "--no-node",
        action="store_true",
        help="skip the frontend checks that need node (image build has no node)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print tracebacks for failed checks",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.no_node:
        args.node = ""
    if args.bridge and not Path(args.bridge).is_file():
        print(f"FAIL: patched bridge.py not found: {args.bridge}", file=sys.stderr)
        return 2
    if args.html and not Path(args.html).is_file():
        print(f"FAIL: built web/index.html not found: {args.html}", file=sys.stderr)
        return 2
    if args.config and not Path(args.config).is_file():
        print(f"FAIL: config.yaml not found: {args.config}", file=sys.stderr)
        return 2

    report = Report(args.verbose)
    ctx = Context(args)
    try:
        # 1. port contract
        report.check("port/config: ingress_port matches ports key, host port and webui",
                     check_config_port_contract, ctx)
        report.check("port/config: ingress + panel_admin are enabled",
                     check_config_ingress_flags, ctx)
        report.check("port/bridge: WEB_PORT is an integer constant",
                     check_bridge_port_constant, ctx)
        report.check("port/bridge: WEB_PORT equals config.yaml ingress_port",
                     check_bridge_port_matches_config, ctx)
        report.check("port/bridge: uvicorn.run passes port=WEB_PORT, proxy_headers=False",
                     check_uvicorn_call, ctx)

        # 2. frontend ingress safety
        report.check("html: no root-absolute asset references",
                     check_html_root_absolute_refs, ctx)
        report.check("html: local CSS/JS assets are inline",
                     check_html_local_assets_inline, ctx)
        report.check("html: fetch wrapper rewrites /api/... to ./api/... + CSRF header",
                     check_fetch_rewrites, ctx)
        report.check("html: rewritten URL stays under the ingress prefix",
                     check_fetch_resolves_under_ingress, ctx)
        report.check("html: rewritten URL resolves on the direct port",
                     check_fetch_resolves_on_direct_port, ctx)

        # 3 + 4. bypass resistance and token non-leakage (needs the runtime)
        report.check("runtime: patched bridge boots on a 1.0.10 /data tree",
                     check_runtime_bootstrap, ctx)
        report.check("bypass: every /api route except POST /api/session is 401",
                     check_all_api_routes_require_auth, ctx)
        report.check("bypass: middleware exempts only POST /api/session",
                     check_middleware_exempts_only_session, ctx)
        report.check("bypass: forged ingress header from non-Supervisor peers is 401",
                     check_forged_ingress_header_rejected, ctx)
        report.check("bypass: Supervisor peer with the ingress header is authorised",
                     check_supervisor_peer_authorised, ctx)
        report.check("bypass: _peer_is_supervisor trusts only 172.30.32.2",
                     check_peer_is_supervisor_strict, ctx)
        report.check("bypass: /docs, /redoc and /openapi.json stay disabled",
                     check_docs_disabled, ctx)
        report.check("token: /api/auth/token is 401 / 403 / 403 / 200 as specified",
                     check_auth_token_requires_ingress, ctx)
        report.check("token: never appears in pages, error bodies or /api/config",
                     check_token_not_leaked, ctx)
        report.check("token: never appears in the startup log",
                     check_token_absent_from_logs, ctx)
        report.check("token: no log call interpolates ACCESS_TOKEN (AST)",
                     check_no_token_log_call, ctx)
        ctx.close_current()

        # 5. legacy /data load
        report.check("legacy: a 1.0.9 config.yaml loads unchanged",
                     check_legacy_config_loads_unchanged, ctx)
        report.check("legacy: _set_token restores identity + micoapi",
                     check_legacy_token_restore, ctx)
        report.check("legacy: GET /api/config masks secrets without dropping keys",
                     check_legacy_config_api_preserves_keys, ctx)
        report.check("legacy: pre-existing /data files are not rewritten",
                     check_legacy_files_untouched, ctx)
    finally:
        ctx.close_all()

    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
