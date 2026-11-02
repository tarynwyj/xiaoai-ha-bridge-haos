"""Upgrade / rollback compatibility of the /data volume, proven by execution.

The add-on stores everything it must keep across updates in the HAOS add-on
``/data`` directory (mounted at ``/app/config`` and ``/app/logs`` inside the
container)::

    /data/config/config.yaml           user configuration
    /data/config/auth.json             saved Xiaomi identity
    /data/config/.mi.token             saved micoapi token
    /data/config/last_timestamp.json   voice-command de-duplication state
    /data/config/.access_token         1.0.10 only: direct-access token

This test builds BOTH released applications from the pinned upstream sources and
the real patch scripts, then executes the patched ``bridge.py`` functions against
in-memory fakes (no network, no miservice, no real credentials, no Docker, no
Home Assistant) to prove:

  S1  a fresh 1.0.9 install produces a realistic /data tree (1.0.9 keys,
      multi-entity intent rules, device aliases, AI keys, saved login files);
  S2  1.0.10 boots against that untouched 1.0.9 /data: every config key/value
      survives, the saved Xiaomi identity (incl. micoapi) is restored,
      GET /api/config masking does not mutate the file on disk, a fresh
      /data/config/.access_token is created and nothing else is touched,
      a masked config round-trips through POST /api/config unchanged;
  S3  the /data 1.0.10 leaves behind is still read by 1.0.9 (rollback): config,
      identity and startup all still work, and 1.0.9 simply ignores
      .access_token (it contains no reference to it at all);
  S4  file shapes after 1.0.10 ran (auth.json exactly 3 keys, .mi.token 4 keys
      with a 2-element micoapi list, config.yaml key set unchanged, no new
      required field);
  S5  corrupted /data: invalid YAML and a non-JSON .mi.token.  The behaviour is
      reported exactly as observed -- if 1.0.10 raises, that is printed as a
      FINDING (with the 1.0.9 comparison) instead of being silently "passed".

Deliberately NOT covered here (reported as NOT VERIFIED by the accompanying
report): the HAOS Supervisor add-on update itself, GHCR image pull/build, the
Ingress side panel, add-on restart, snapshot/restore, real MiService and real
Home Assistant.

Usage::

    python test_upgrade_data.py --pristine-bridge ../upstream-pinned/bridge.py \
        --pristine-html ../upstream-pinned/web/index.html --repo . [--require-legacy]

``--require-legacy`` turns "1.0.9 sources were unavailable in git history" from a
skip (exit 0) into a failure (exit 1); CI passes it together with
``fetch-depth: 0``.  All credentials below are synthetic.
"""

import argparse
import ast
import asyncio as real_asyncio
import copy
import datetime as real_datetime
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import ModuleType, SimpleNamespace

import yaml  # already a runtime dependency of bridge.py (requirements.lock.txt)

MASKED = "\u2022" * 8
LEGACY_REF_DEFAULT = "d2fdbce"  # parent of the 1.0.10 commit == last 1.0.9 state
EXPECTED_UPSTREAM_MARKER = 'VERSION = "3.9.0"'
SYNTHETIC_DEVICE = {"deviceID": "speaker-1", "hardware": "L05C", "name": "客厅音箱"}
SYNTHETIC_RECORD = {
    "time": 1_700_000_000_000,
    "query": "打开客厅灯",
    "answer": "好的",
}

# Every value here is synthetic.  The shape is what a real QR-login install of
# 1.0.9 leaves on disk: no username/password, credentials in cookie_text, plus a
# user-edited multi-entity rule, device aliases and AI settings.
LEGACY_CONFIG = {
    "xiaomi": {
        "username": "",
        "password": "",
        "cookie_text": (
            "userId=qr-user; passToken=qr-pass; "
            "serviceToken=valid-service; deviceId=qr-device"
        ),
        "device_did": "speaker-1",
        "hardware": "L05C",
        "tts_reply": True,
    },
    "homeassistant": {"url": "http://homeassistant:80", "token": "ha-secret-token"},
    "openai": {
        "api_base": "https://api.example.invalid/v1",
        "api_key": "sk-synthetic-key",
        "model": "gpt-4o-mini",
    },
    "ai_mode": True,
    "device_aliases": {"客厅灯": "light.ceiling", "台灯": "switch.desk"},
    "poll_interval_seconds": 2,
    "intent_rules": [
        {
            "pattern": "打开客厅灯",
            "action": {
                "domain": "light",
                "service": "turn_on",
                "entity_id": ["light.ceiling", "light.floor"],
                "reply": "好的",
            },
        },
        {
            "pattern": "打开台灯",
            "action": {
                "domain": "switch",
                "service": "turn_on",
                "entity_id": "switch.desk",
                "reply": "好的，台灯已打开",
            },
        },
    ],
}

BRIDGE_STATE_TEMPLATE = {
    "running": True,
    "connected": False,
    "last_command": None,
    "last_command_time": None,
    "command_count": 0,
    "error": None,
    "start_time": None,
    "devices": None,
}

# ───────────────────────── patched-AST function sets ─────────────────────────
# Functions every patched bridge.py must expose for this test to run.
BASE_FUNCTIONS = (
    "load_config",
    "save_config",
    "_default_config",
    "_set_token",
    "_save_auth_and_token",
    "_get_cookie",
    "_load_saved_xiaomi_identity",
    "_refresh_micoapi_token",
    "post_config",
    "bridge_loop",
)
# Only 1.0.10 has these (the access-control / credential-hardening release).
NEW_ONLY_FUNCTIONS = (
    "_read_saved_credential",
    "_token_snapshot",
    "_restore_token",
    "_apply_cookie_to_session",
    "_relogin_and_refresh",
    "_merge_xiaomi_credentials",
    "_delete_saved_login_files",
    "get_config",
    "_config_section_revision",
    "_load_or_create_access_token",
)
# Module-level assignments needed by the functions above.  ACCESS_TOKEN is the
# import-time side effect that creates /data/config/.access_token in 1.0.10.
NEEDED_ASSIGNMENTS = (
    "AUTH_TOKEN_PATH",
    "MASKED_VALUE",
    "RELOGIN_BACKOFF_START",
    "RELOGIN_BACKOFF_MAX",
    "ACCESS_TOKEN",
    "SESSION_VALUE",
)


class Reporter:
    def __init__(self):
        self.checks = []
        self.findings = []
        self.skips = []

    def info(self, message):
        print(message, flush=True)

    def ok(self, message):
        self.checks.append(message)
        print(f"  ok   {message}", flush=True)

    def note(self, message):
        print(f"  note {message}", flush=True)

    def finding(self, message):
        self.findings.append(message)
        print(f"  FINDING {message}", flush=True)

    def skip(self, message):
        self.skips.append(message)
        print(f"  SKIP {message}", flush=True)


class Logger:
    """Captures formatted log lines instead of writing files."""

    def __init__(self):
        self.messages = []

    def _add(self, level, message, args):
        text = message % args if args else message
        self.messages.append(f"[{level}] {text}")

    def debug(self, message, *args):
        self._add("DEBUG", message, args)

    def info(self, message, *args):
        self._add("INFO", message, args)

    def warning(self, message, *args):
        self._add("WARNING", message, args)

    def error(self, message, *args):
        self._add("ERROR", message, args)

    def exception(self, message, *args):
        self._add("EXCEPTION", message, args)

    def has(self, needle):
        return any(needle in message for message in self.messages)


# ───────────────────────── fakes (design mirrors test_startup_patch.py) ─────────────────────────
class FakeCookieJar:
    def __init__(self):
        self.cleared = []
        self.updated = []

    def clear_domain(self, domain):
        self.cleared.append(domain)

    def update_cookies(self, jar, url):
        self.updated.append((jar, url))


class FakeSession:
    def __init__(self):
        self.closed = False
        self.cookie_jar = FakeCookieJar()

    async def close(self):
        self.closed = True


class FakeAccount:
    """Mimics miservice.MiAccount closely enough for the login paths."""

    instances = []

    def __init__(self, session, username, password, token_store=None):
        # 1.0.9 and 1.0.10 both must keep MiService away from .mi.token: MiService
        # deletes its token file when a login fails.
        assert token_store is None, "the app must never hand .mi.token to MiService"
        self.session = session
        self.username = username
        self.password = password
        self.token = None
        self.service_login_calls = []
        FakeAccount.instances.append(self)

    async def _serviceLogin(self, uri, data=None):
        self.service_login_calls.append(uri)
        # Echo the account identity the caller just put on the token, like the
        # real service returns the same account back.
        token = self.token if isinstance(self.token, dict) else {}
        return {
            "code": 0,
            "location": "https://sts.example.invalid/sts",
            "nonce": "1234567",
            "ssecurity": "ssecurity-value",
            "userId": token.get("userId") or "user-1",
            "passToken": token.get("passToken") or "pass-token-rotated",
        }

    async def _securityTokenService(self, location, nonce, ssecurity):
        return "fresh-service-token"

    async def login(self, sid):
        raise AssertionError("the SMS password login path must not be used here")


class FakeMiNAService:
    created = []
    said = []
    VALID_SERVICE_TOKENS = ("valid-service", "fresh-service-token")

    def __init__(self, account):
        self.account = account
        FakeMiNAService.created.append(self)

    async def device_list(self):
        token = self.account.token
        if not isinstance(token, dict):
            raise RuntimeError("HTTP 401 (token cleared)")
        micoapi = token.get("micoapi") or ("", "")
        service = micoapi[1] if len(micoapi) > 1 else ""
        if service not in self.VALID_SERVICE_TOKENS:
            self.account.token = None
            raise RuntimeError("HTTP 401")
        return [dict(SYNTHETIC_DEVICE)]

    async def text_to_speech(self, device_id, text):
        FakeMiNAService.said.append((device_id, text))


class FakeHAClient:
    created = []
    calls = []

    def __init__(self, url, token):
        self.url = url
        self.token = token
        FakeHAClient.created.append(self)

    async def test_connection(self, session):
        return True

    async def call_service(self, domain, service, data, session):
        FakeHAClient.calls.append((domain, service, dict(data)))
        return True


class FakeStack:
    """asyncio / polling seams for one patched application."""

    def __init__(self, log, bridge_state, sleeps, poll_script):
        self.log = log
        self.bridge_state = bridge_state
        self.sleeps = sleeps
        self.poll_script = poll_script

    async def sleep(self, delay):
        self.sleeps.append(delay)
        # One sleep is enough: stop the poll loop instead of polling forever.
        self.bridge_state["running"] = False

    def create_task(self, coro):
        coro.close()

        class FinishedTask:
            def done(self):
                return True
            def cancel(self):
                return None
            def add_done_callback(self, callback):
                callback(self)
        return FinishedTask()

    async def poll(self, session, device_id, hardware, last_ts):
        records = self.poll_script.pop(0) if self.poll_script else []
        return records, last_ts, False

    async def poll_mina(self, na, device_id):
        return []

    async def automation(self, *args, **kwargs):
        return "xiaoai_bridge_test", True

    async def delayed(self, *args, **kwargs):
        return None

    async def ai(self, query):
        return None


def install_module_stubs():
    """Install the third-party modules the patched code imports at call time."""
    miservice = ModuleType("miservice")
    miservice.MiAccount = FakeAccount
    miservice.MiNAService = FakeMiNAService
    miaccount = ModuleType("miservice.miaccount")
    miaccount.get_random = lambda length: "R" * length
    aiohttp_stub = ModuleType("aiohttp")
    aiohttp_stub.ClientSession = FakeSession
    aiohttp_stub.CookieJar = FakeCookieJar
    aiohttp_stub.ClientTimeout = lambda **kwargs: None
    yarl_stub = ModuleType("yarl")
    yarl_stub.URL = lambda text: text
    sys.modules["miservice"] = miservice
    sys.modules["miservice.miaccount"] = miaccount
    sys.modules["aiohttp"] = aiohttp_stub
    sys.modules["yarl"] = yarl_stub


def reset_fakes():
    FakeAccount.instances.clear()
    FakeMiNAService.created.clear()
    FakeMiNAService.said.clear()
    FakeHAClient.created.clear()
    FakeHAClient.calls.clear()


# ───────────────────────── one patched application ─────────────────────────
class PatchedApp:
    """A built add-on (``bridge.py``) executed against one synthetic /data tree."""

    def __init__(self, app_dir: Path, data_dir: Path, label: str):
        self.label = label
        self.app_dir = Path(app_dir)
        self.data_dir = Path(data_dir)
        self.config_dir = self.data_dir / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "logs").mkdir(parents=True, exist_ok=True)
        self.config_path = self.config_dir / "config.yaml"
        self.token_path = self.config_dir / ".mi.token"
        self.auth_path = self.config_dir / "auth.json"
        self.last_ts_path = self.config_dir / "last_timestamp.json"
        self.access_token_path = self.config_dir / ".access_token"
        self.source = (self.app_dir / "bridge.py").read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)
        self.is_new = "AUTH_TOKEN_PATH" in self.source
        self.log = Logger()
        self.bridge_state = dict(BRIDGE_STATE_TEMPLATE)
        self.sleeps = []
        self.poll_script = []
        self.namespace = self._build_namespace()

    # -- construction -------------------------------------------------------
    def _build_namespace(self):
        wanted = list(BASE_FUNCTIONS)
        if self.is_new:
            wanted += list(NEW_ONLY_FUNCTIONS)
        functions = {
            node.name: node
            for node in self.tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        missing = [name for name in wanted if name not in functions]
        assert not missing, (
            f"{self.label}: patched bridge.py is missing expected functions: {missing}"
        )

        stack = FakeStack(self.log, self.bridge_state, self.sleeps, self.poll_script)

        def persist_synthetic_credentials(creds):
            identity = {key: creds.get(key, "") for key in ("userId", "passToken", "deviceId")}
            token = dict(identity)
            token["micoapi"] = [creds.get("ssecurity", ""), creds.get("serviceToken", "")]
            self.auth_path.write_text(json.dumps(identity), encoding="utf-8")
            self.token_path.write_text(json.dumps(token), encoding="utf-8")
            cfg = self.load_config()
            cfg.setdefault("xiaomi", {})["cookie_text"] = (
                "userId=" + identity["userId"] + "; passToken=" + identity["passToken"]
                + "; serviceToken=" + creds.get("serviceToken", "")
                + "; deviceId=" + identity["deviceId"]
            )
            self.save_config(cfg)

        async def verified_service_login(session, uri, data=None, device_id="",
                                         token=None, user_agent=""):
            # Synthetic reply for the *verified* refresh transport, not the
            # pinned MiService _serviceLogin() implementation (ssl=False).
            state = token if isinstance(token, dict) else {}
            return {
                "code": 0,
                "location": "https://api2.mina.mi.com/sts",
                "nonce": "1234567",
                "ssecurity": "ssecurity-value",
                "userId": state.get("userId") or "user-1",
                "passToken": state.get("passToken") or "pass-token-rotated",
            }

        async def immediate_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        namespace = {
            "_delayed_tasks": set(),
            "_mi_service_login_verified": verified_service_login,
            "_persist_sms_credentials": persist_synthetic_credentials,
            "_credential_write_allowed": lambda: True,
            # The add-on resolves these relative to its working directory
            # (/app); here they point at the synthetic /data tree instead.
            "CONFIG_PATH": self.config_path,
            "LOG_PATH": self.data_dir / "logs" / "bridge.log",
            "TOKEN_PATH": self.token_path,
            "AUTH_PATH": self.auth_path,
            "COOKIE_TEMPLATE": (
                "deviceId={device_id}; serviceToken={service_token}; "
                "userId={user_id}"
            ),
            "GET_ASK_BY_MINA": ["M01"],
            "VERSION": "test",
            "json": json,
            "re": re,
            "os": os,
            "yaml": yaml,
            "time": time,
            "secrets": secrets,
            "hmac": hmac,
            "hashlib": hashlib,
            "ipaddress": ipaddress,
            "datetime": real_datetime.datetime,
            "log": self.log,
            "bridge_state": self.bridge_state,
            "aiohttp": SimpleNamespace(ClientSession=FakeSession),
            "asyncio": SimpleNamespace(
                sleep=stack.sleep,
                create_task=stack.create_task,
                to_thread=immediate_thread,
                CancelledError=real_asyncio.CancelledError,
                TimeoutError=real_asyncio.TimeoutError,
            ),
            "HAClient": FakeHAClient,
            "_get_latest_ask_from_xiaoai": stack.poll,
            "_get_latest_ask_by_mina": stack.poll_mina,
            "_extract_query": lambda rec: rec.get("query"),
            "_extract_answer": lambda rec: rec.get("answer"),
            "_create_ha_automation": stack.automation,
            "_delayed_call": stack.delayed,
            "ai_parse_command": stack.ai,
            "yarl": SimpleNamespace(URL=lambda text: text),
        }
        # Function annotations are evaluated when the AST node is executed, so
        # pydantic/fastapi model names must resolve to *something*.
        for node in self.tree.body:
            if isinstance(node, ast.ClassDef) and node.name != "IntentParser":
                namespace.setdefault(node.name, object)

        intent_node = next(
            node
            for node in self.tree.body
            if isinstance(node, ast.ClassDef) and node.name == "IntentParser"
        )
        exec(
            compile(
                ast.Module(body=[intent_node], type_ignores=[]),
                "<patched bridge.py>",
                "exec",
            ),
            namespace,
        )
        for name in wanted:
            node = functions[name]
            # FastAPI decorators cannot be evaluated here; keep the bodies.
            node.decorator_list = []
            exec(
                compile(
                    ast.Module(body=[node], type_ignores=[]),
                    f"<{self.label} bridge.py>",
                    "exec",
                ),
                namespace,
            )
        # Module-level assignments run in source order, exactly like an import:
        # AUTH_TOKEN_PATH must exist before ACCESS_TOKEN is created from it.
        for node in self.tree.body:
            if not isinstance(node, ast.Assign):
                continue
            targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if any(target in NEEDED_ASSIGNMENTS for target in targets):
                exec(
                    compile(
                        ast.Module(body=[node], type_ignores=[]),
                        f"<{self.label} bridge.py>",
                        "exec",
                    ),
                    namespace,
                )
        return namespace

    # -- helpers ------------------------------------------------------------
    def load_config(self):
        return self.namespace["load_config"]()

    def save_config(self, cfg):
        return self.namespace["save_config"](cfg)

    def read_json(self, path: Path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def run_loop(self, poll_script=()):
        """Run one bridge_loop() start with fakes; returns self."""
        reset_fakes()
        self.sleeps.clear()
        self.poll_script[:] = list(poll_script)
        self.bridge_state.clear()
        self.bridge_state.update(BRIDGE_STATE_TEMPLATE)
        real_asyncio.run(self.namespace["bridge_loop"]())
        return self

    def post_config(self, config):
        request = SimpleNamespace(config=copy.deepcopy(config))
        return real_asyncio.run(self.namespace["post_config"](request))

    def get_config(self):
        return real_asyncio.run(self.namespace["get_config"]())


# ───────────────────────── build helpers ─────────────────────────
def git_show(repo: Path, ref: str, rel_path: str):
    """Return the bytes of ``ref:rel_path`` or None when unavailable."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "show", f"{ref}:{rel_path}"],
            capture_output=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    return result.stdout


def write_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def build_app(
    root: Path,
    pristine_bridge: Path,
    pristine_html: Path,
    web_script: Path,
    auth_script: Path,
    reporter: Reporter,
    label: str,
) -> Path:
    """Build one released app: pristine upstream + real patch scripts."""
    (root / "web").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    pristine_text = pristine_bridge.read_text(encoding="utf-8")
    pristine_html_text = pristine_html.read_text(encoding="utf-8")
    (root / "bridge.py").write_text(pristine_text, encoding="utf-8")
    (root / "web" / "index.html").write_text(pristine_html_text, encoding="utf-8")
    for script, target in (
        (web_script, root / "web" / "index.html"),
        (auth_script, root / "bridge.py"),
    ):
        command = [sys.executable, str(script), str(target)]
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", cwd=str(root)
        )
        reporter.info(f"  build {label}: exit {result.returncode} | {' '.join(command)}")
        if result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                reporter.info(f"         {line}")
        assert result.returncode == 0, (
            f"{label}: patch script {script.name} failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    patched = (root / "bridge.py").read_text(encoding="utf-8")
    assert patched != pristine_text, f"{label}: patch_auth.py did not change bridge.py"
    assert (root / "web" / "index.html").read_text(encoding="utf-8") != pristine_html_text, (
        f"{label}: patch_web.py did not change web/index.html"
    )
    return root


def extract_legacy_scripts(
    repo: Path, ref: str, destination: Path, reporter: Reporter
):
    """Write the 1.0.9 patch scripts from git history; return (auth, web) or None."""
    scripts = {}
    missing = []
    for name in ("patch_auth.py", "patch_web.py"):
        data = git_show(repo, ref, f"xiaoai_bridge/{name}")
        if data is None:
            missing.append(f"git show {ref}:xiaoai_bridge/{name}")
            continue
        path = destination / name
        write_bytes(path, data)
        scripts[name] = path
    if missing:
        reporter.info(f"  legacy 1.0.9 sources unavailable: {'; '.join(missing)}")
        return None
    version_blob = git_show(repo, ref, "xiaoai_bridge/config.yaml")
    if version_blob is not None:
        version_text = version_blob.decode("utf-8", "replace")
        match = re.search(r'^version:\s*"([^"]+)"', version_text, re.MULTILINE)
        if match:
            reporter.info(f"  legacy sources from {ref} report add-on version {match.group(1)}")
            assert match.group(1) == "1.0.9", (
                f"{ref} is not the last 1.0.9 state (config.yaml version "
                f"{match.group(1)!r}); pass --legacy-ref to target the right commit"
            )
    return scripts["patch_auth.py"], scripts["patch_web.py"]


# ───────────────────────── assertions ─────────────────────────
def flatten_into(value, prefix, out):
    if isinstance(value, dict):
        for key, item in value.items():
            flatten_into(item, f"{prefix}.{key}" if prefix else str(key), out)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            flatten_into(item, f"{prefix}[{index}]", out)
    else:
        out[prefix] = value
    return out


def flatten(value):
    return flatten_into(value, "", {})


def assert_same_config(actual, expected, label):
    actual_flat = flatten(actual)
    expected_flat = flatten(expected)
    dropped = sorted(set(expected_flat) - set(actual_flat))
    assert not dropped, f"{label}: 1.0.9 keys dropped: {dropped}"
    added = sorted(set(actual_flat) - set(expected_flat))
    assert not added, f"{label}: unexpected new keys: {added}"
    changed = {
        key: (expected_flat[key], actual_flat[key])
        for key in expected_flat
        if actual_flat[key] != expected_flat[key]
    }
    assert not changed, f"{label}: values changed: {changed}"


def snapshot_files(data_dir: Path):
    """name -> sha256 for every saved state file present under config/."""
    result = {}
    for path in sorted((data_dir / "config").glob("*")):
        if path.is_file():
            result[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def assert_unchanged(data_dir: Path, snapshot, names, label):
    for name in names:
        path = data_dir / "config" / name
        assert path.exists(), f"{label}: {name} was deleted"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == snapshot[name], f"{label}: {name} was modified"


# ───────────────────────── scenarios ─────────────────────────
def scenario_1_legacy_install(legacy: PatchedApp, reporter: Reporter):
    """S1: a fresh 1.0.9 install produces a realistic /data tree."""
    data = legacy.data_dir
    assert not legacy.config_path.exists(), "S1: /data must start empty"

    # 1a: no config at all -> the app writes its default config.yaml.
    legacy.run_loop()
    assert legacy.config_path.exists(), "S1a: 1.0.9 must create config/config.yaml"
    defaults = legacy.load_config()
    assert legacy.bridge_state["error"], "S1a: startup without credentials must be refused"
    for key in (
        "xiaomi",
        "homeassistant",
        "openai",
        "ai_mode",
        "device_aliases",
        "poll_interval_seconds",
        "intent_rules",
    ):
        assert key in defaults, f"S1a: default 1.0.9 config is missing {key}"
    assert not legacy.token_path.exists() and not legacy.auth_path.exists()
    assert not legacy.access_token_path.exists(), "S1a: 1.0.9 must not create .access_token"
    reporter.ok("S1a fresh 1.0.9 install creates a default config.yaml and refuses to start")

    # 1b: the user saves a realistic configuration through the 1.0.9 API.
    result = legacy.post_config(LEGACY_CONFIG)
    assert result == {"ok": True}, f"S1b: unexpected POST /api/config result {result!r}"
    assert_same_config(legacy.load_config(), LEGACY_CONFIG, "S1b")
    reporter.ok("S1b 1.0.9 POST /api/config stores a realistic config (multi-entity rules, aliases, AI keys)")

    # 1c: start-up logs in with the QR cookie and writes the saved state files.
    legacy.run_loop(poll_script=[[dict(SYNTHETIC_RECORD)]])
    assert legacy.bridge_state["error"] is None, f"S1c: {legacy.bridge_state['error']}"
    devices = legacy.bridge_state["devices"]
    assert devices and devices[0]["deviceID"] == "speaker-1", devices
    assert legacy.log.has("已使用保存的 micoapi Token 登录"), legacy.log.messages
    assert legacy.bridge_state["command_count"] == 1
    assert FakeHAClient.calls, "S1c: the voice command must reach Home Assistant"
    domain, service, payload = FakeHAClient.calls[0]
    assert (domain, service) == ("light", "turn_on"), (domain, service)
    assert payload["entity_id"] == ["light.ceiling", "light.floor"], payload

    auth = legacy.read_json(legacy.auth_path)
    assert auth == {
        "passToken": "qr-pass",
        "userId": "qr-user",
        "deviceId": "qr-device",
    }, auth
    token = legacy.read_json(legacy.token_path)
    assert set(token) == {"passToken", "userId", "deviceId", "micoapi"}, token
    assert token["micoapi"] == ["", "valid-service"], token
    assert legacy.read_json(legacy.last_ts_path) == {
        "speaker-1": SYNTHETIC_RECORD["time"],
    }, legacy.read_json(legacy.last_ts_path)
    assert not legacy.access_token_path.exists(), "S1c: 1.0.9 must not create .access_token"
    reporter.ok(
        "S1c 1.0.9 start-up writes auth.json / .mi.token / last_timestamp.json "
        "and runs a multi-entity voice command"
    )
    return snapshot_files(data)


def scenario_2_upgrade(new: PatchedApp, legacy_snapshot, reporter: Reporter):
    """S2: 1.0.10 boots against the untouched 1.0.9 /data tree."""
    data = new.data_dir
    assert new.is_new, "S2: the 'new' app must be the 1.0.10 patched build"

    # Constructing PatchedApp runs the module-level ACCESS_TOKEN creation, which
    # is exactly what importing the real 1.0.10 bridge.py does at start-up.
    assert new.access_token_path.exists(), (
        "S2a: 1.0.10 must create /data/config/.access_token on first start"
    )
    access_token = new.access_token_path.read_text(encoding="utf-8").strip()
    assert new.namespace["ACCESS_TOKEN"] == access_token, "S2a: in-memory token != file"
    assert re.fullmatch(r"[A-Za-z0-9_-]{43,}", access_token), (
        f"S2a: .access_token is not a 256-bit urlsafe token (len={len(access_token)})"
    )
    mode = new.access_token_path.stat().st_mode & 0o777
    if os.name == "posix":
        assert mode == 0o600, f"S2a: .access_token must be 0600, got {oct(mode)}"
        reporter.ok("S2a 1.0.10 creates a 256-bit .access_token (mode 0600) on first start")
    else:
        reporter.note(
            f".access_token mode is {oct(mode)} on {os.name}; 0600 is only enforced "
            "on Linux (Docker image / CI), verified there"
        )
        reporter.ok("S2a 1.0.10 creates a fresh 256-bit .access_token on first start")
    assert_unchanged(
        data,
        legacy_snapshot,
        ("config.yaml", "auth.json", ".mi.token", "last_timestamp.json"),
        "S2a",
    )
    assert set(snapshot_files(data)) == set(legacy_snapshot) | {".access_token"}, (
        "S2a: 1.0.10 created/deleted unexpected files: "
        f"{sorted(set(snapshot_files(data)) ^ (set(legacy_snapshot) | {'.access_token'}))}"
    )
    reporter.ok("S2a .access_token creation left every 1.0.9 file byte-identical")

    # Every 1.0.9 key/value must load unchanged.
    loaded = new.load_config()
    assert_same_config(loaded, LEGACY_CONFIG, "S2b")
    reporter.ok("S2b 1.0.10 load_config() returns every 1.0.9 key with the same value")

    # The saved Xiaomi identity must be restored from .mi.token (incl. micoapi).
    account = FakeAccount(None, "", "")
    new.namespace["_set_token"](account, loaded)
    assert account.token == {
        "passToken": "qr-pass",
        "userId": "qr-user",
        "deviceId": "qr-device",
        "micoapi": ("", "valid-service"),
    }, account.token
    reporter.ok("S2c 1.0.10 _set_token() restores userId/passToken/deviceId/micoapi from .mi.token")

    # GET /api/config must mask without mutating the file on disk.
    config_bytes = new.config_path.read_bytes()
    masked_cfg = new.get_config()
    # This install logged in by QR scan, so password is legitimately empty and is
    # left empty; every other secret must be masked.
    assert masked_cfg["xiaomi"]["password"] == "", masked_cfg["xiaomi"]
    assert masked_cfg["xiaomi"]["cookie_text"] == MASKED, masked_cfg["xiaomi"]
    assert masked_cfg["homeassistant"]["token"] == MASKED, masked_cfg["homeassistant"]
    assert masked_cfg["openai"]["api_key"] == MASKED, masked_cfg["openai"]
    assert new.config_path.read_bytes() == config_bytes, (
        "S2d: GET /api/config mutated config.yaml on disk"
    )
    assert_same_config(new.load_config(), LEGACY_CONFIG, "S2d")
    reporter.ok("S2d GET /api/config masks secrets and leaves config.yaml byte-identical")

    # 1.0.10 start-up against the 1.0.9 data: no reconfiguration required.
    new.run_loop()
    assert new.bridge_state["error"] is None, f"S2e: {new.bridge_state['error']}"
    assert new.bridge_state["devices"], "S2e: no device discovered"
    assert new.log.has("已使用保存的 micoapi Token 登录"), new.log.messages
    assert not FakeHAClient.calls, "S2e: no new voice record => no HA call"
    assert_unchanged(
        data,
        legacy_snapshot,
        ("config.yaml", "last_timestamp.json"),
        "S2e",
    )
    assert new.read_json(new.auth_path) == {
        "passToken": "qr-pass",
        "userId": "qr-user",
        "deviceId": "qr-device",
    }, new.read_json(new.auth_path)
    assert new.read_json(new.token_path)["micoapi"] == ["", "valid-service"]
    assert new.access_token_path.read_text(encoding="utf-8").strip() == access_token
    reporter.ok(
        "S2e 1.0.10 starts on the 1.0.9 /data (cached micoapi login) without touching "
        "config.yaml / last_timestamp.json / .access_token"
    )

    # Masked config round-trip through POST /api/config (collect -> save).
    masked_cfg = new.get_config()
    result = new.post_config(masked_cfg)
    assert result.get("ok") is True, f"S2f: POST /api/config failed: {result!r}"
    reloaded = new.load_config()
    assert_same_config(reloaded, LEGACY_CONFIG, "S2f")
    multi = reloaded["intent_rules"][0]["action"]["entity_id"]
    assert multi == ["light.ceiling", "light.floor"], (
        f"S2f: multi-entity intent rule changed: {multi!r}"
    )
    assert reloaded["xiaomi"]["cookie_text"] == LEGACY_CONFIG["xiaomi"]["cookie_text"]
    assert reloaded["homeassistant"]["token"] == "ha-secret-token"
    assert reloaded["openai"]["api_key"] == "sk-synthetic-key"
    assert new.access_token_path.read_text(encoding="utf-8").strip() == access_token
    reporter.ok(
        "S2f a masked config round-trips through 1.0.10 POST /api/config "
        "(multi-entity rules and every secret preserved)"
    )


def scenario_4_shapes(new: PatchedApp, reporter: Reporter):
    """S4: on-disk shapes after 1.0.10 ran."""
    auth = new.read_json(new.auth_path)
    assert set(auth) == {"passToken", "userId", "deviceId"}, auth
    assert auth["userId"] == "qr-user" and auth["passToken"] == "qr-pass", auth
    reporter.ok("S4a auth.json still has exactly {passToken, userId, deviceId}")

    token = new.read_json(new.token_path)
    assert set(token) == {"passToken", "userId", "deviceId", "micoapi"}, token
    assert isinstance(token["micoapi"], list) and len(token["micoapi"]) == 2, token
    assert token["micoapi"][1], f"S4b: empty micoapi serviceToken: {token!r}"
    reporter.ok("S4b .mi.token has userId/passToken/deviceId and a 2-element micoapi list")

    config = new.load_config()
    new_keys = set(flatten(config)) - set(flatten(LEGACY_CONFIG))
    assert not new_keys, f"S4c: 1.0.10 added config keys 1.0.9 would not understand: {new_keys}"
    assert set(flatten(LEGACY_CONFIG)) - set(flatten(config)) == set()
    reporter.ok("S4c config.yaml key set is unchanged (no new key 1.0.9 would choke on)")

    # No new *required* field: 1.0.10 booted on the untouched 1.0.9 config in
    # S2e, and load_config() injects nothing that is absent from the file.
    assert flatten(config) == flatten(LEGACY_CONFIG), "S4d: load_config() rewrote values"
    reporter.ok("S4d 1.0.10 booted with the 1.0.9 config unchanged => no new required field")


def scenario_3_rollback(legacy: PatchedApp, upgraded_snapshot, reporter: Reporter):
    """S3: 1.0.9 still reads the /data that 1.0.10 left behind."""
    assert not legacy.is_new, "S3: the rollback app must be the 1.0.9 patched build"
    assert ".access_token" not in legacy.source, (
        "S3a: the 1.0.9 bridge.py unexpectedly references .access_token"
    )
    assert "AUTH_TOKEN_PATH" not in legacy.source, (
        "S3a: the 1.0.9 bridge.py unexpectedly defines AUTH_TOKEN_PATH"
    )
    assert legacy.access_token_path.exists(), (
        "S3a: constructing 1.0.9 deleted the 1.0.10 .access_token"
    )
    access_token = legacy.access_token_path.read_text(encoding="utf-8").strip()
    reporter.ok("S3a 1.0.9 has no .access_token/AUTH_TOKEN_PATH code and leaves the file alone")

    loaded = legacy.load_config()
    assert_same_config(loaded, LEGACY_CONFIG, "S3b")
    reporter.ok("S3b 1.0.9 load_config() still reads every key/value written by 1.0.10")

    account = FakeAccount(None, "", "")
    legacy.namespace["_set_token"](account, loaded)
    assert account.token == {
        "passToken": "qr-pass",
        "userId": "qr-user",
        "deviceId": "qr-device",
        "micoapi": ("", "valid-service"),
    }, account.token
    reporter.ok("S3c 1.0.9 _set_token() still restores userId/passToken/deviceId/micoapi")

    legacy.run_loop()
    assert legacy.bridge_state["error"] is None, f"S3d: {legacy.bridge_state['error']}"
    assert legacy.bridge_state["devices"], "S3d: 1.0.9 did not discover the device"
    assert legacy.log.has("已使用保存的 micoapi Token 登录"), legacy.log.messages
    assert legacy.bridge_state["command_count"] == 0
    assert legacy.access_token_path.read_text(encoding="utf-8").strip() == access_token, (
        "S3d: 1.0.9 rewrote/removed the 1.0.10 .access_token"
    )
    assert legacy.read_json(legacy.auth_path) == {
        "passToken": "qr-pass",
        "userId": "qr-user",
        "deviceId": "qr-device",
    }, legacy.read_json(legacy.auth_path)
    assert legacy.read_json(legacy.token_path)["micoapi"] == ["", "valid-service"]
    reporter.ok(
        "S3d 1.0.9 starts normally on the /data left by 1.0.10 "
        "(rollback works, .access_token ignored)"
    )


def scenario_5_corrupted_data(
    temp_root: Path,
    new_app_dir: Path,
    legacy_app_dir,
    source_data: Path,
    reporter: Reporter,
):
    """S5: dishonest-free look at corrupted /data."""
    corrupt_root = temp_root / "corrupt"
    corrupt_root.mkdir(parents=True, exist_ok=True)

    def clone(name):
        destination = corrupt_root / name
        shutil.copytree(source_data, destination)
        return destination

    # ── S5a: invalid YAML in config.yaml ──────────────────────────────────
    bad_yaml = b"xiaomi: [unclosed\nhomeassistant:\n  url: http://homeassistant:80\n"
    data_a = clone("invalid-yaml")
    (data_a / "config" / "config.yaml").write_bytes(bad_yaml)
    app_a = PatchedApp(new_app_dir, data_a, "1.0.10 with invalid YAML")
    load_error = None
    try:
        app_a.load_config()
    except Exception as exc:  # noqa: BLE001 - the point is to observe it
        load_error = exc
    loop_error = None
    try:
        app_a.run_loop()
    except Exception as exc:  # noqa: BLE001 - the point is to observe it
        loop_error = exc
    if load_error is None:
        reporter.ok("S5a invalid YAML: 1.0.10 load_config() fell back without raising")
    else:
        detail = " ".join(str(load_error).split())
        reporter.finding(
            "S5a invalid YAML: 1.0.10 load_config() raises "
            f"{type(load_error).__name__} ({detail}) instead of falling back to defaults; "
            "bridge_loop() "
            + (
                f"raises {type(loop_error).__name__} as well"
                if loop_error is not None
                else f"does not raise (bridge_state error={app_a.bridge_state['error']!r})"
            )
            + ". The bad file is left untouched; not fixed here, reported to the Lead."
        )
    assert (data_a / "config" / "config.yaml").read_bytes() == bad_yaml, (
        "S5a: 1.0.10 overwrote the corrupt config.yaml"
    )
    assert app_a.token_path.exists() and app_a.auth_path.exists(), (
        "S5a: 1.0.10 deleted saved login files while handling a corrupt config"
    )
    if load_error is not None:
        reporter.ok("S5a invalid YAML: the corrupt file and the saved login files are preserved")

    if legacy_app_dir is not None:
        data_l = clone("invalid-yaml-legacy")
        (data_l / "config" / "config.yaml").write_bytes(bad_yaml)
        app_l = PatchedApp(legacy_app_dir, data_l, "1.0.9 with invalid YAML")
        legacy_error = None
        try:
            app_l.load_config()
        except Exception as exc:  # noqa: BLE001 - comparison only
            legacy_error = exc
        if (legacy_error is None) == (load_error is None):
            reporter.ok(
                "S5a invalid YAML: 1.0.9 behaves identically => not a 1.0.10 regression"
            )
        else:
            reporter.finding(
                "S5a invalid YAML: 1.0.9 and 1.0.10 differ "
                f"(1.0.9 -> {type(legacy_error).__name__ if legacy_error else 'no error'}, "
                f"1.0.10 -> {type(load_error).__name__ if load_error else 'no error'})"
            )

    # ── S5b: .mi.token is not JSON (auth.json still fine) ────────────────
    bad_token = b"{this is not json"
    data_b = clone("invalid-token")
    (data_b / "config" / ".mi.token").write_bytes(bad_token)
    app_b = PatchedApp(new_app_dir, data_b, "1.0.10 with a non-JSON .mi.token")
    account = FakeAccount(None, "", "")
    app_b.namespace["_set_token"](account, app_b.load_config())
    assert isinstance(account.token, dict), (
        f"S5b: _set_token() did not fall back to auth.json (token={account.token!r})"
    )
    assert account.token["userId"] == "qr-user", account.token
    assert account.token["passToken"] == "qr-pass", account.token
    assert "micoapi" not in account.token, account.token
    assert (data_b / "config" / ".mi.token").read_bytes() == bad_token, (
        "S5b: _set_token() deleted/overwrote the unreadable .mi.token"
    )
    reporter.ok(
        "S5b non-JSON .mi.token: 1.0.10 _set_token() falls back to auth.json and "
        "leaves the bad file alone"
    )

    app_b.run_loop()
    assert app_b.bridge_state["error"] is None, f"S5b: {app_b.bridge_state['error']}"
    assert app_b.log.has("passToken"), app_b.log.messages
    repaired = app_b.read_json(app_b.token_path)
    assert repaired["micoapi"][1] == "fresh-service-token", repaired
    assert repaired["userId"] == "qr-user" and repaired["passToken"] == "qr-pass", repaired
    reporter.ok(
        "S5b non-JSON .mi.token: 1.0.10 refreshes the passToken and repairs .mi.token "
        "in place (bridge starts)"
    )


def forward_only_data(new: PatchedApp, reporter: Reporter):
    """Build a valid 1.0.10 /data when the 1.0.9 sources are unavailable."""
    new.run_loop()
    assert new.config_path.exists(), "forward-only: 1.0.10 must create config/config.yaml"
    result = new.post_config(LEGACY_CONFIG)
    assert result.get("ok") is True, f"forward-only: POST /api/config failed: {result!r}"
    new.run_loop(poll_script=[[dict(SYNTHETIC_RECORD)]])
    assert new.bridge_state["error"] is None, (
        f"forward-only: {new.bridge_state['error']}"
    )
    assert new.access_token_path.exists()
    reporter.ok(
        "forward-only: 1.0.10 fresh install produces a valid /data tree "
        "(config + saved login + .access_token)"
    )


# ───────────────────────── main ─────────────────────────
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Execute 1.0.9 -> 1.0.10 -> 1.0.9 /data upgrade and rollback scenarios "
            "against the pinned upstream sources and the real patch scripts."
        )
    )
    parser.add_argument(
        "--pristine-bridge",
        required=True,
        type=Path,
        help="pristine upstream bridge.py (commit e4c82d4b...)",
    )
    parser.add_argument(
        "--pristine-html",
        required=True,
        type=Path,
        help="pristine upstream web/index.html (commit e4c82d4b...)",
    )
    parser.add_argument(
        "--repo",
        required=True,
        type=Path,
        help="add-on repo root (or the xiaoai_bridge/ directory) with current patch scripts",
    )
    parser.add_argument(
        "--require-legacy",
        action="store_true",
        help="fail instead of skipping when the 1.0.9 sources are not in git history",
    )
    parser.add_argument(
        "--legacy-ref",
        default=LEGACY_REF_DEFAULT,
        help=f"git ref of the last 1.0.9 state (default: {LEGACY_REF_DEFAULT})",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    reporter = Reporter()

    pristine_bridge = args.pristine_bridge.resolve()
    pristine_html = args.pristine_html.resolve()
    repo = args.repo.resolve()
    addon_dir = repo / "xiaoai_bridge"
    if not (addon_dir / "patch_auth.py").exists():
        addon_dir = repo
    current_auth = addon_dir / "patch_auth.py"
    current_web = addon_dir / "patch_web.py"

    reporter.info(
        f"upgrade data test | repo={repo} | pristine bridge={pristine_bridge} | "
        f"pristine html={pristine_html} | legacy ref={args.legacy_ref}"
    )
    for path in (pristine_bridge, pristine_html, current_auth, current_web):
        assert path.exists(), f"missing required input: {path}"
    pristine_source = pristine_bridge.read_text(encoding="utf-8")
    if EXPECTED_UPSTREAM_MARKER not in pristine_source:
        reporter.note(
            f"pristine bridge.py does not contain {EXPECTED_UPSTREAM_MARKER!r}; "
            "the patch scripts will fail loudly if it is not the pinned upstream"
        )

    install_module_stubs()
    temp_root = Path(tempfile.mkdtemp(prefix="xiaoai-upgrade-data-"))
    reporter.info(f"  temp workspace: {temp_root}")
    try:
        apps_root = temp_root / "apps"
        new_app_dir = build_app(
            apps_root / "1.0.10",
            pristine_bridge,
            pristine_html,
            current_web,
            current_auth,
            reporter,
            "1.0.10",
        )

        legacy_scripts = extract_legacy_scripts(
            repo, args.legacy_ref, temp_root / "legacy-scripts", reporter
        )
        if legacy_scripts is None:
            reason = (
                f"could not read the 1.0.9 patch scripts from git history "
                f"(ref {args.legacy_ref}); a shallow clone needs "
                "git fetch --unshallow / actions/checkout fetch-depth: 0"
            )
            if args.require_legacy:
                reporter.info(f"  FAIL {reason} and --require-legacy was given")
                return 1
            reporter.skip(f"S1-S4 legacy 1.0.9 round-trip: {reason}")

            data_forward = temp_root / "data" / "forward-only"
            new = PatchedApp(new_app_dir, data_forward, "1.0.10")
            forward_only_data(new, reporter)
            scenario_5_corrupted_data(
                temp_root, new_app_dir, None, data_forward, reporter
            )
        else:
            legacy_auth, legacy_web = legacy_scripts
            legacy_app_dir = build_app(
                apps_root / "1.0.9",
                pristine_bridge,
                pristine_html,
                legacy_web,
                legacy_auth,
                reporter,
                "1.0.9",
            )

            data_dir = temp_root / "data" / "upgrade"
            legacy = PatchedApp(legacy_app_dir, data_dir, "1.0.9")
            legacy_snapshot = scenario_1_legacy_install(legacy, reporter)

            new = PatchedApp(new_app_dir, data_dir, "1.0.10")
            scenario_2_upgrade(new, legacy_snapshot, reporter)
            scenario_4_shapes(new, reporter)

            upgraded_snapshot = snapshot_files(data_dir)
            rollback = PatchedApp(legacy_app_dir, data_dir, "1.0.9 rollback")
            scenario_3_rollback(rollback, upgraded_snapshot, reporter)

            scenario_5_corrupted_data(
                temp_root, new_app_dir, legacy_app_dir, data_dir, reporter
            )
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    if reporter.findings:
        print(flush=True)
        print("findings (observed, NOT fixed here - reported to the Lead):", flush=True)
        for finding in reporter.findings:
            print(f"  - {finding}", flush=True)
    print(
        f"upgrade data tests passed ({len(reporter.checks)} checks, "
        f"{len(reporter.findings)} findings, {len(reporter.skips)} skipped)",
        flush=True,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        sys.exit(main())
    except AssertionError:
        traceback.print_exc()
        print("FAIL: upgrade data test assertion failed", file=sys.stderr, flush=True)
        sys.exit(1)
