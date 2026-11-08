"""Isolated behavioural tests for the patched bridge.

The tests run against the *patched* ``bridge.py`` (produces by ``patch_auth.py``)
without installing miservice and without any network access: every I/O seam is
replaced by an in-memory fake, and the real functions under test are lifted from
the AST so they execute as the shipped code does.

Covered scenarios (see task list C/D/E):
  1.  normal start with a still-valid cached micoapi token
  2.  cached token expired -> passToken refresh -> start succeeds
  3.  QR / Cookie only install (no username + password) logs in
  4.  runtime 401 -> refresh succeeds -> polling resumes and a command executes
  5.  runtime 401 -> refresh fails -> exponential backoff, credentials untouched
  6.  _save_auth_and_token never blanks a valid credential
  7.  post_config: masked / stale-page save keeps secrets, explicit clear wipes
  8.  multi-entity intent rules survive the parser (regression G, backend side)

Usage: python test_startup_patch.py [/app/bridge.py]
"""

import ast
import asyncio as real_asyncio
import datetime as real_datetime
import json
import re
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

source_path = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/bridge.py")
source = source_path.read_text(encoding="utf-8")
tree = ast.parse(source)
assert 'IntentParser(load_config().get("intent_rules", [])).parse(query)' in source, (
    "patched bridge.py must re-read intent rules on every command"
)

MASKED = "\u2022" * 8
WANTED_FUNCS = [
    "_set_token",
    "_build_token_account",
    "_load_saved_xiaomi_identity",
    "_get_cookie",
    "_valid_saved_cookie_text",
    "_write_last_timestamp_atomic",
    "_saved_credential_pair_conflicted",
    "_refresh_micoapi_token",
    "_token_snapshot",
    "_restore_token",
    "_apply_cookie_to_session",
    "_poll_auth_cookies",
    "_looks_like_schedule",
    "_parse_schedule_command",
    "_schedule_now",
    "_parse_relative_offset",
    "_cn_int",
    "_parse_cn_clock",
    "_apply_period",
    "_parse_schedule_days_cn",
    "_parse_once_date",
    "_pick_schedule_device",
    "_schedule_service_for",
    "_relogin_and_refresh",
    "_relogin_failure_from",
    "_relogin_retry_plan",
    "_credential_fingerprint",
    "_wait_for_relogin",
    "_refresh_interval_seconds",
    "_token_age_seconds",
    "_poll_targets",
    "_warn_poll_scope_empty",
    "_read_saved_credential",
    "_save_auth_and_token",
    "_merge_xiaomi_credentials",
    "_delete_saved_login_files",
    "_enforce_access_control",
    "post_config",
    "bridge_loop",
    "_plausible_credential",
    "_adopt_login_identity",
    "_login_device_id",
    "_refresh_lock_obj",
    "_refresh_outcome",
    "_recent_refresh",
    "_remember_refresh",
    "_unsaved_refresh",
    "_save_refreshed_credentials",
]
nodes = {}
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in WANTED_FUNCS:
        nodes[node.name] = node
missing = [name for name in WANTED_FUNCS if name not in nodes]
assert not missing, f"patched bridge.py is missing expected functions: {missing}"

rejected_class = next(
    (node for node in tree.body
     if isinstance(node, ast.ClassDef) and node.name == "MicoapiLoginRejected"),
    None,
)
assert rejected_class is not None, (
    "patched bridge.py must classify a rejected exchange with MicoapiLoginRejected"
)
identity_class = next(
    (node for node in tree.body
     if isinstance(node, ast.ClassDef) and node.name == "CredentialIdentityMismatch"),
    None,
)
assert identity_class is not None, (
    "patched bridge.py must refuse a mismatched account identity with "
    "CredentialIdentityMismatch"
)

# FastAPI decorators cannot be evaluated here; keep the plain function bodies.
for name in ("post_config", "bridge_loop"):
    nodes[name].decorator_list = []


# ───────────────────────── fakes ─────────────────────────
class Logger:
    def __init__(self):
        self.messages = []
        self.level = "INFO"

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
    reject_service_login = False
    transport_error = False

    def __init__(self, session, username, password, token_store=None):
        assert token_store is None, "MiService must never own the saved token file"
        self.session = session
        self.username = username
        self.password = password
        self.token_store = token_store
        self.token = None
        self.service_login_calls = []
        self.instances.append(self)

    async def _serviceLogin(self, uri, data=None):
        self.service_login_calls.append(uri)
        if FakeAccount.reject_service_login:
            return {"code": 70016, "description": "passToken 已失效"}
        return {
            "code": 0,
            "location": "https://sts.example.invalid/sts",
            "nonce": "1234567",
            "ssecurity": "ssecurity-value",
            "userId": "user-1",
            "passToken": "pass-token-rotated",
        }

    async def _securityTokenService(self, location, nonce, ssecurity):
        if FakeAccount.reject_service_login:
            raise RuntimeError("STS rejected")
        return "fresh-service-token"

    async def login(self, sid):
        raise AssertionError("legacy MiAccount.login() must not be used for recovery")


class FakeMiNAService:
    devices = None  # None -> the default single speaker

    def __init__(self, account):
        self.account = account
        FakeMiNAService.created.append(self)

    created = []

    async def device_list(self):
        token = self.account.token
        if not isinstance(token, dict):
            raise RuntimeError("HTTP 401 (token cleared)")
        micoapi = token.get("micoapi") or ("", "")
        service = micoapi[1] if len(micoapi) > 1 else ""
        if service not in ("valid-service", "fresh-service-token"):
            # Emulate miservice: a 401 clears the in-memory token.
            self.account.token = None
            raise RuntimeError("HTTP 401")
        if FakeMiNAService.devices is not None:
            return list(FakeMiNAService.devices)
        return [{"deviceID": "speaker-1", "hardware": "L05C", "name": "客厅音箱"}]

    async def text_to_speech(self, device_id, text):
        FakeMiNAService.said.append(text)


FakeMiNAService.created = []
FakeMiNAService.said = []


class FakeHAClient:
    created = []
    calls = []
    stop_after_connect = False
    state = None

    def __init__(self, url, token):
        assert url == "http://homeassistant:80"
        assert token == "ha-secret"
        self.url = url
        self.token = token
        FakeHAClient.created.append(self)

    async def test_connection(self, session):
        if FakeHAClient.stop_after_connect:
            FakeHAClient.state["running"] = False
        return True

    async def call_service(self, domain, service, data, session):
        FakeHAClient.calls.append((domain, service, dict(data)))
        FakeHAClient.state["running"] = False
        return True


def install_miservice_stubs():
    miservice = ModuleType("miservice")
    miservice.MiAccount = FakeAccount
    miservice.MiNAService = FakeMiNAService
    miaccount = ModuleType("miservice.miaccount")
    miaccount.get_random = lambda length: "R" * length
    sys.modules["miservice"] = miservice
    sys.modules["miservice.miaccount"] = miaccount
    yarl = ModuleType("yarl")
    yarl.URL = lambda text: text
    sys.modules["yarl"] = yarl


class Harness:
    """One isolated patched-bridge runtime with its own /data tree."""

    def __init__(self, root: Path):
        self.root = root
        self.token_path = root / "config" / ".mi.token"
        self.auth_path = root / "config" / "auth.json"
        self.cfg_path = root / "config" / "config.yaml"
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg = {
            "xiaomi": {
                "username": "",
                "password": "",
                "cookie_text": "",
                "device_did": "speaker-1",
                "hardware": "L05C",
                "tts_reply": True,
            },
            "homeassistant": {"url": "http://homeassistant:80", "token": "ha-secret"},
            "intent_rules": [],
            "poll_interval_seconds": 2,
        }
        self.saved_configs = []
        self.sleeps = []
        self.verified_calls = []
        self.polled_devices = []
        self.log = Logger()
        self.poll_script = []
        self.bridge_state = {
            "running": True,
            "connected": False,
            "error": None,
            "devices": None,
            "command_count": 0,
        }
        self.account_hooks = {}
        self.login_needs_otp = False
        self.otp_pending = False
        self.otp_cleanups = 0
        FakeHAClient.state = self.bridge_state
        self.namespace = self._build_namespace()

    def _fake_asyncio(self):
        sleeps = self.sleeps

        async def sleep(delay):
            sleeps.append(delay)
            if len(sleeps) > 50:
                raise AssertionError("the poll loop did not terminate within 50 iterations")

        class FinishedTask:
            def done(self):
                return True

            def cancel(self):
                return None

            def add_done_callback(self, callback):
                callback(self)

        def create_task(coro):
            coro.close()
            return FinishedTask()

        async def to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        return SimpleNamespace(
            sleep=sleep,
            create_task=create_task,
            to_thread=to_thread,
            CancelledError=real_asyncio.CancelledError,
            TimeoutError=real_asyncio.TimeoutError,
            Lock=real_asyncio.Lock,
        )

    async def fake_poll(self, session, device_id, hardware, last_ts):
        self.polled_devices.append(device_id)
        if self.poll_script:
            records, need_relogin = self.poll_script.pop(0)
        else:
            records, need_relogin = ([], False)
        return records, last_ts, need_relogin

    async def fake_automation(*args, **kwargs):
        return "xiaoai_bridge_test", True

    async def fake_delayed(*args, **kwargs):
        return None

    async def fake_ai(query):
        return None

    def _build_namespace(self):
        async def clear_synthetic_login():
            for p in (self.auth_path, self.token_path):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
            mi = self.cfg.setdefault("xiaomi", {})
            mi["password"] = ""
            mi["cookie_text"] = ""
            return {"ok": True}

        def persist_synthetic_credentials(creds):
            if not creds.get("userId") or not creds.get("passToken") or not creds.get("serviceToken"):
                raise ValueError("incomplete")
            identity = {key: creds.get(key, "") for key in ("userId", "passToken", "deviceId")}
            token = dict(identity)
            token["micoapi"] = [creds.get("ssecurity", ""), creds["serviceToken"]]
            # Mirror production: only a new serviceToken restamps issued_at.
            previous = {}
            try:
                previous = json.loads(self.token_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                previous = {}
            previous_micoapi = previous.get("micoapi")
            if (isinstance(previous_micoapi, list) and len(previous_micoapi) == 2
                    and previous_micoapi[1] == creds["serviceToken"]
                    and isinstance(previous.get("issued_at"), (int, float))):
                token["issued_at"] = int(previous["issued_at"])
            else:
                token["issued_at"] = int(time.time())
            self.auth_path.write_text(json.dumps(identity), encoding="utf-8")
            self.token_path.write_text(json.dumps(token), encoding="utf-8")
            self.cfg.setdefault("xiaomi", {})["cookie_text"] = (
                "userId=" + identity["userId"] + "; passToken=" + identity["passToken"]
                + "; serviceToken=" + creds["serviceToken"]
            )

        async def fake_otp_password_login(username, password):
            if self.login_needs_otp:
                self.otp_pending = True
                return {"ok": True, "need_otp": True, "verified": False}
            from miservice import MiAccount
            account = MiAccount(FakeSession(), username, password, None)
            account.token = {"deviceId": "OTPDEVICE", "userId": "user-1",
                             "passToken": "pass-token-rotated"}
            account.token["micoapi"] = ("ssecurity-value", "fresh-service-token")
            persist_synthetic_credentials({
                "userId": account.token["userId"], "passToken": account.token["passToken"],
                "deviceId": account.token["deviceId"], "ssecurity": "ssecurity-value",
                "serviceToken": "fresh-service-token"
            })
            return {"ok": True, "verified": True, "devices": [{"deviceID": "speaker-1", "hardware": "L05C"}]}

        async def reset_otp_login():
            self.otp_pending = False
            self.otp_cleanups += 1

        async def verified_service_login(session, uri, data=None, device_id="", token=None,
                                         user_agent=""):
            # Simulate the TLS-verified transport, not the pinned library's
            # _serviceLogin implementation (which explicitly disables TLS).
            self.verified_calls.append({"device_id": device_id,
                                        "token": dict(token or {})})
            if FakeAccount.transport_error:
                raise RuntimeError("访问小米账号服务失败（网络错误）")
            if FakeAccount.reject_service_login:
                return {"code": 70016, "description": "passToken 已失效"}
            return {
                "code": 0, "location": "https://api2.mina.mi.com/sts",
                "nonce": "1234567", "ssecurity": "ssecurity-value",
                "userId": (token or {}).get("userId", "user-1"),
                "passToken": "pass-token-rotated",
            }

        namespace = {
            "_mi_service_login_verified": verified_service_login,
            "_persist_sms_credentials": persist_synthetic_credentials,
            "_credential_write_allowed": lambda: True,
            "_delayed_tasks": set(),
            "_last_refresh_failure": {},
            "clear_xiaomi_login": clear_synthetic_login,
            "_otp_password_login": fake_otp_password_login,
            "_reset_otp_state": reset_otp_login,
            "TOKEN_PATH": self.token_path,
            "AUTH_PATH": self.auth_path,
            "CONFIG_PATH": self.cfg_path,
            "COOKIE_TEMPLATE": "deviceId={device_id}; serviceToken={service_token}; userId={user_id}",
            "MASKED_VALUE": MASKED,
            "RELOGIN_BACKOFF_START": 10,
            "RELOGIN_BACKOFF_MAX": 300,
            "GET_ASK_BY_MINA": ["M01"],
            "json": json,
            "re": re,
            "os": __import__("os"),
            "time": __import__("time"),
            "REFRESH_COALESCE_SECONDS": 60,
            "_refresh_lock": None,
            "_poll_scope_warned": "",
            "_process_login_device_id": "",
            "_refresh_cache": {},
            "datetime": real_datetime.datetime,
            "log": self.log,
            "load_config": self.load_config,
            "save_config": self.save_config,
            "aiohttp": SimpleNamespace(ClientSession=FakeSession),
            "asyncio": self._fake_asyncio(),
            "HAClient": FakeHAClient,
            "bridge_state": self.bridge_state,
            "_get_latest_ask_from_xiaoai": self.fake_poll,
            "_extract_query": lambda rec: rec.get("query"),
            "_extract_answer": lambda rec: rec.get("answer"),
            "_create_ha_automation": self.fake_automation,
            "_delayed_call": self.fake_delayed,
            "ai_parse_command": self.fake_ai,
            "SaveConfigRequest": object,
            "TestHARequest": object,
            "SessionRequest": object,
            "Request": object,
            "JSONResponse": object,
            "HTMLResponse": object,
        }
        # Module level constants the extracted functions read at call time
        # (e.g. POLL_COOKIE_KEYS). Explicit values above win.
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                name = node.targets[0].id
                if name.isupper() and name not in namespace:
                    try:
                        namespace[name] = ast.literal_eval(node.value)
                    except (ValueError, SyntaxError):
                        pass

        intent_node = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "IntentParser"
        )
        exec(
            compile(ast.Module(body=[intent_node], type_ignores=[]), str(source_path), "exec"),
            namespace,
        )
        exec(
            compile(ast.Module(body=[rejected_class], type_ignores=[]), str(source_path), "exec"),
            namespace,
        )
        exec(
            compile(ast.Module(body=[identity_class], type_ignores=[]), str(source_path), "exec"),
            namespace,
        )
        for name in WANTED_FUNCS:
            if name in ("post_config", "bridge_loop", "_enforce_access_control"):
                continue
            module = ast.Module(body=[nodes[name]], type_ignores=[])
            exec(compile(module, str(source_path), "exec"), namespace)
        for name in ("post_config", "bridge_loop"):
            module = ast.Module(body=[nodes[name]], type_ignores=[])
            exec(compile(module, str(source_path), "exec"), namespace)
        return namespace

    def load_config(self):
        return self.cfg

    def save_config(self, new_cfg):
        self.saved_configs.append(json.loads(json.dumps(new_cfg)))
        self.cfg = new_cfg

    def write_token(self, data):
        self.token_path.write_text(json.dumps(data), encoding="utf-8")

    def write_auth(self, data):
        self.auth_path.write_text(json.dumps(data), encoding="utf-8")

    def read_token(self):
        return json.loads(self.token_path.read_text(encoding="utf-8"))

    def run(self):
        real_asyncio.run(self.namespace["bridge_loop"]())


# ───────────────────────── scenarios ─────────────────────────
def scenario_normal_start(root: Path):
    harness = Harness(root)
    harness.write_token(
        {
            "userId": "user-1",
            "passToken": "pass-1",
            "deviceId": "account-device",
            "micoapi": ["ssecurity-value", "valid-service"],
        }
    )
    harness.write_auth(
        {"userId": "user-1", "passToken": "pass-1", "deviceId": "account-device"}
    )
    before = harness.read_token()

    harness.run()

    assert harness.bridge_state["error"] is None, harness.bridge_state["error"]
    assert harness.bridge_state["devices"][0]["deviceID"] == "speaker-1"
    assert harness.log.has("已使用保存的 micoapi Token 登录")
    # An unstamped token (upgrade from an older version) is renewed once so the
    # renewal clock starts from a known point; the account identity must not move.
    after = harness.read_token()
    assert (after["userId"], after["passToken"], after["deviceId"]) == (
        before["userId"], before["passToken"], before["deviceId"]
    ), "a proactive renewal must never change the account identity"
    assert after["micoapi"][1], "the renewed token must still be usable"
    return "normal start with valid cached token"


def scenario_startup_refresh(root: Path):
    harness = Harness(root)
    harness.write_token(
        {
            "userId": "user-1",
            "passToken": "pass-1",
            "deviceId": "account-device",
            "micoapi": ["old-ssecurity", "expired-service"],
        }
    )
    harness.write_auth(
        {"userId": "user-1", "passToken": "pass-1", "deviceId": "account-device"}
    )

    harness.run()

    assert harness.bridge_state["error"] is None, harness.bridge_state["error"]
    assert harness.log.has("passToken 登录") or harness.log.has("尝试 passToken")
    saved = harness.read_token()
    assert saved["micoapi"][1] == "fresh-service-token", saved
    assert saved["passToken"] == "pass-token-rotated", saved
    return "expired cached token refreshed through passToken"


def scenario_qr_only_start(root: Path):
    """QR login leaves cookie_text + files but no username/password (issue C)."""
    harness = Harness(root)
    harness.cfg["xiaomi"]["cookie_text"] = (
        "userId=qr-user; passToken=qr-pass; serviceToken=valid-service; deviceId=qr-device"
    )

    harness.run()

    assert harness.bridge_state["error"] is None, harness.bridge_state["error"]
    assert harness.bridge_state["devices"][0]["deviceID"] == "speaker-1"
    assert harness.auth_path.exists(), "auth.json must be created after QR-only login"
    assert harness.token_path.exists(), ".mi.token must be created after QR-only login"
    saved = harness.read_token()
    assert saved["userId"] == "qr-user"
    assert saved["passToken"] == "qr-pass"
    assert saved["micoapi"][1] == "valid-service"
    return "QR-only install (no username/password) logs in"


def scenario_runtime_refresh_recovers(root: Path):
    harness = Harness(root)
    harness.write_token(
        {
            "userId": "user-1",
            "passToken": "pass-1",
            "deviceId": "account-device",
            "micoapi": ["old-ssecurity", "expired-service"],
        }
    )
    harness.cfg["intent_rules"] = [
        {
            "pattern": "打开客厅灯",
            "action": {
                "domain": "light",
                "service": "turn_on",
                "entity_id": ["light.ceiling", "light.floor"],
                "reply": "好的",
            },
        }
    ]
    # First poll says "token expired", second one delivers the command.
    harness.poll_script = [
        ([], True),
        (
            [
                {
                    "time": 1_700_000_000_000,
                    "query": "打开客厅灯",
                    "answer": "好的",
                }
            ],
            False,
        ),
    ]

    harness.run()

    assert harness.log.has("Token 刷新成功"), harness.log.messages
    assert FakeHAClient.calls, "the command after recovery must reach Home Assistant"
    domain, service, data = FakeHAClient.calls[0]
    assert (domain, service) == ("light", "turn_on")
    assert data["entity_id"] == ["light.ceiling", "light.floor"], data
    assert harness.bridge_state["command_count"] == 1
    assert harness.read_token()["micoapi"][1] == "fresh-service-token"
    # Recovery must not spin: the sleep after login is the poll interval.
    assert 2 in harness.sleeps, harness.sleeps
    return "runtime 401 -> refresh -> polling resumes and command executes"


def scenario_scheduled_rule_never_executes_now(root: Path):
    for index, query in enumerate(("明天八点打开客厅灯", "十分钟后打开客厅灯",
                                    "每天早上七点打开客厅灯", "每天二十五点打开客厅灯")):
        harness = Harness(root / str(index))
        harness.write_token({"userId": "user-1", "passToken": "pass-1",
                             "deviceId": "account-device", "micoapi": ["s", "valid-service"],
                             "issued_at": int(time.time())})
        targets = ["light.ceiling", "light.floor"]
        harness.cfg["intent_rules"] = [{
            "pattern": "(打开|开)(客厅灯|客厅的灯)",
            "action": {"domain": "light", "service": "turn_on", "entity_id": targets,
                       "brightness_pct": 35, "reply": "好的"}}]
        harness.poll_script = [([{"time": 1_700_000_000_000,
                                  "query": query, "answer": "好的"}], False)]
        scheduled = []
        async def capture(ha, domain, service, data, at, days, session):
            scheduled.append((domain, service, dict(data), at, days))
            return "xiaoai_bridge_test", True
        async def stop(delay):
            harness.bridge_state["running"] = False
        harness.namespace["_create_ha_automation"] = capture
        harness.namespace["asyncio"].sleep = stop
        if index == 3:
            harness.cfg["ai_mode"] = True
            harness.cfg["openai"] = {"api_key": "synthetic-key"}
            async def unsafe_ai(query):
                return {"domain": "light", "service": "turn_on", "entity_id": "light.ceiling"}
            harness.namespace["ai_parse_command"] = unsafe_ai
        FakeHAClient.calls.clear()
        harness.run()
        assert not FakeHAClient.calls, (query, FakeHAClient.calls)
        assert harness.bridge_state["error"] is None, harness.log.messages
        if index == 3:
            assert not scheduled, (query, scheduled)
        else:
            assert len(scheduled) == 1, (query, harness.log.messages)
            assert scheduled[0][2]["entity_id"] == targets, scheduled
            assert scheduled[0][2]["brightness_pct"] == 35, scheduled
            assert scheduled[0][2]["once"] is (index < 2), scheduled
    return "scheduled speech keeps time and rule parameters instead of executing now"


def scenario_schedule_ai_fallback_keeps_time(root: Path):
    harness = Harness(root)
    harness.cfg["ai_mode"] = True
    harness.cfg["openai"] = {"api_key": "synthetic-key"}
    harness.write_token({"userId": "user-1", "passToken": "pass-1",
                         "deviceId": "account-device", "micoapi": ["s", "valid-service"],
                         "issued_at": int(time.time())})
    harness.poll_script = [([{"time": 1_700_000_000_000,
                              "query": "每天七点打开书房灯"}], False)]
    scheduled = []
    async def empty_catalog(*args):
        return []
    async def safe_ai(query):
        return {"domain": "light", "service": "turn_on", "entity_id": "light.study",
                "type": "schedule", "schedule_time": "07:00", "schedule_days": "daily"}
    async def capture(*args):
        scheduled.append(args)
        return "xiaoai_bridge_test", True
    async def stop(delay):
        harness.bridge_state["running"] = False
    harness.namespace["_ha_device_catalog"] = empty_catalog
    harness.namespace["ai_parse_command"] = safe_ai
    harness.namespace["_create_ha_automation"] = capture
    harness.namespace["asyncio"].sleep = stop
    harness.run()
    assert len(scheduled) == 1 and not FakeHAClient.calls, (scheduled, harness.log.messages)
    return "valid AI schedule fallback remains available without permitting immediate actions"


def scenario_renewal_updates_device_scope(root: Path):
    harness = Harness(root)
    harness.cfg["xiaomi"]["device_did"] = ""
    harness.write_token({"userId": "user-1", "passToken": "pass-1",
                         "deviceId": "account-device", "micoapi": ["s", "valid-service"],
                         "issued_at": 0})
    harness.write_auth({"userId": "user-1", "passToken": "pass-1", "deviceId": "account-device"})
    discoveries = []
    original = FakeMiNAService.device_list
    async def discover(service):
        devices = [{"deviceID": "speaker-1", "hardware": "L05C"}]
        if discoveries:
            devices.append({"deviceID": "speaker-2", "hardware": "L05C"})
        discoveries.append(devices)
        return devices
    async def stop(delay):
        harness.sleeps.append(delay)
        if len(harness.sleeps) >= 4:
            harness.bridge_state["running"] = False
    FakeMiNAService.device_list = discover
    harness.namespace["asyncio"].sleep = stop
    try:
        harness.run()
    finally:
        FakeMiNAService.device_list = original
    assert "speaker-2" in harness.polled_devices, (discoveries, harness.polled_devices)
    assert len(harness.bridge_state["devices"]) == 2, harness.bridge_state
    return "newly discovered speakers join both polling and the displayed device cache"


def scenario_unsaved_renewal_retries_persistence(root: Path):
    harness = Harness(root)
    harness.write_token({"userId": "user-1", "passToken": "pass-1",
                         "deviceId": "account-device", "micoapi": ["s", "valid-service"],
                         "issued_at": 0})
    harness.write_auth({"userId": "user-1", "passToken": "pass-1", "deviceId": "account-device"})
    persist = harness.namespace["_persist_sms_credentials"]
    writes = []
    clock = [time.time()]
    observed = []
    def flaky(creds):
        if creds["serviceToken"] == "fresh-service-token" and not writes:
            writes.append("failed")
            raise OSError("disk full")
        persist(creds)
    async def advance(delay):
        observed.append(harness.namespace["_poll_auth_cookies"]("speaker-1"))
        clock[0] += 301
        if len(observed) >= 3:
            harness.bridge_state["running"] = False
    harness.namespace["_persist_sms_credentials"] = flaky
    harness.namespace["time"] = SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic)
    harness.namespace["asyncio"].sleep = advance
    harness.run()
    assert len(harness.verified_calls) == 1, harness.verified_calls
    assert observed[0]["serviceToken"] == "fresh-service-token", observed
    assert "passToken" not in observed[0], observed
    assert harness.read_token()["passToken"] == "pass-token-rotated", harness.read_token()
    assert harness.bridge_state.get("renewal") is None, harness.bridge_state
    return "a failed renewal write is retried after five minutes while polling uses the new token"


def scenario_runtime_refresh_failure(root: Path):
    harness = Harness(root)
    # Startup succeeds from the cached token; only the runtime refresh fails.
    harness.write_token(
        {
            "userId": "user-1",
            "passToken": "pass-1",
            "deviceId": "account-device",
            "micoapi": ["old-ssecurity", "valid-service"],
        }
    )
    harness.write_auth(
        {"userId": "user-1", "passToken": "pass-1", "deviceId": "account-device"}
    )
    token_before = harness.read_token()
    auth_before = json.loads(harness.auth_path.read_text(encoding="utf-8"))
    harness.poll_script = [([], True), ([], True), ([], True)]
    # A network failure is transient: it must keep the exponential backoff.
    FakeAccount.transport_error = True

    # The loop would retry forever; stop it after the second backoff sleep.
    async def bounded_sleep(delay):
        harness.sleeps.append(delay)
        if len(harness.sleeps) >= 2:
            harness.bridge_state["running"] = False

    harness.namespace["asyncio"].sleep = bounded_sleep
    try:
        harness.run()
    finally:
        FakeAccount.transport_error = False

    assert token_before["passToken"] == harness.read_token()["passToken"], (
        "failed refresh must not rewrite .mi.token"
    )
    assert token_before["micoapi"] == harness.read_token()["micoapi"], (
        "failed refresh must keep the working service token"
    )
    assert auth_before == json.loads(harness.auth_path.read_text(encoding="utf-8")), (
        "failed refresh must not rewrite auth.json"
    )
    assert harness.token_path.exists(), "failed refresh must never delete .mi.token"
    backoffs = [delay for delay in harness.sleeps if delay >= 10]
    assert backoffs[:2] == [10, 20], f"expected exponential backoff, got {harness.sleeps}"
    assert harness.bridge_state["error"], "a persistent failure must be surfaced"
    return "runtime 401 -> transient refresh failure -> exponential backoff"


def scenario_runtime_refresh_rejected(root: Path):
    """A rejected passToken must not be retried on the 5-minute ladder forever."""
    harness = Harness(root)
    harness.write_token(
        {
            "userId": "user-1",
            "passToken": "pass-1",
            "deviceId": "account-device",
            "micoapi": ["old-ssecurity", "valid-service"],
        }
    )
    harness.write_auth(
        {"userId": "user-1", "passToken": "pass-1", "deviceId": "account-device"}
    )
    token_before = harness.read_token()
    harness.poll_script = [([], True), ([], True), ([], True)]
    FakeAccount.reject_service_login = True

    async def bounded_sleep(delay):
        harness.sleeps.append(delay)
        if len(harness.sleeps) >= 2:
            harness.bridge_state["running"] = False

    harness.namespace["asyncio"].sleep = bounded_sleep
    try:
        harness.run()
    finally:
        FakeAccount.reject_service_login = False

    assert token_before["passToken"] == harness.read_token()["passToken"], (
        "a rejected refresh must not rewrite .mi.token"
    )
    assert token_before["micoapi"] == harness.read_token()["micoapi"], (
        "a rejected refresh must keep the working service token"
    )
    message = harness.bridge_state["error"] or ""
    assert "70016" in message, f"the panel must show the Xiaomi code, got {message!r}"
    assert "重新扫码" in message, f"the panel must name the fix, got {message!r}"
    assert "正在自动重试" not in message, (
        f"a rejected passToken must not claim it is retrying, got {message!r}"
    )
    backoffs = [delay for delay in harness.sleeps if delay >= 10]
    assert 10 not in backoffs and 20 not in backoffs, (
        f"a rejected passToken must not use the retry ladder, got {harness.sleeps}"
    )
    assert backoffs, f"the loop must still wait before the next attempt, got {harness.sleeps}"
    return "runtime 401 -> rejected passToken -> 'log in again', no retry ladder"


def scenario_proactive_renewal_keeps_session_alive(root: Path):
    """An expiring session must be renewed before it dies, with no 401 and no alarm.

    The whole point: as long as a renewal succeeds every few hours, the user never
    has to scan the QR code again. A renewal failure while the current token still
    works must stay a log line, not a panel error.
    """
    harness = Harness(root)
    harness.write_token(
        {
            "userId": "user-1",
            "passToken": "pass-1",
            "deviceId": "account-device",
            "micoapi": ["old-ssecurity", "valid-service"],
            "issued_at": int(time.time()) - 10 * 3600,
        }
    )
    harness.write_auth(
        {"userId": "user-1", "passToken": "pass-1", "deviceId": "account-device"}
    )
    token_before = harness.read_token()
    # The poll never returns 401: the renewal must happen purely on the clock.
    harness.poll_script = [([], False), ([], False), ([], False)]

    async def bounded_sleep(delay):
        harness.sleeps.append(delay)
        if len(harness.sleeps) >= 2:
            harness.bridge_state["running"] = False

    harness.namespace["asyncio"].sleep = bounded_sleep
    harness.run()

    token_after = harness.read_token()
    assert token_after["micoapi"][1] == "fresh-service-token", (
        f"an old token must be renewed without waiting for a 401, got {token_after}"
    )
    assert token_after["issued_at"] > token_before["issued_at"], (
        "the renewal must restamp issued_at so the next one is scheduled from it"
    )
    assert harness.bridge_state["error"] is None, (
        f"a successful renewal must not raise a panel error, got {harness.bridge_state['error']!r}"
    )
    return "an old token is renewed on schedule: no 401, no panel error"


def scenario_renewal_failure_stays_quiet(root: Path):
    """A failed renewal while the token still works must not alarm the user."""
    harness = Harness(root)
    harness.write_token(
        {
            "userId": "user-1",
            "passToken": "pass-1",
            "deviceId": "account-device",
            "micoapi": ["old-ssecurity", "valid-service"],
            "issued_at": int(time.time()) - 10 * 3600,
        }
    )
    harness.write_auth(
        {"userId": "user-1", "passToken": "pass-1", "deviceId": "account-device"}
    )
    harness.poll_script = [([], False), ([], False), ([], False)]
    FakeAccount.transport_error = True

    async def bounded_sleep(delay):
        harness.sleeps.append(delay)
        if len(harness.sleeps) >= 3:
            harness.bridge_state["running"] = False

    harness.namespace["asyncio"].sleep = bounded_sleep
    try:
        harness.run()
    finally:
        FakeAccount.transport_error = False

    assert harness.bridge_state["error"] is None, (
        "a failed renewal must not become a panel error while polling still works, "
        f"got {harness.bridge_state['error']!r}"
    )
    assert harness.log.has("登录态续期失败"), "the failure must still be visible in the log"
    attempts = [m for m in harness.log.messages if "登录态主动续期" in m]
    assert len(attempts) == 1, (
        f"a failed renewal must back off, not retry on every poll, got {attempts}"
    )
    return "a failed renewal retries quietly instead of demanding a new login"


def scenario_device_id_stays_stable(root: Path):
    """A missing deviceId must not become a brand-new device on every exchange.

    Xiaomi treats an unknown deviceId as a new device login; the community fix for
    persistent 70016 is to pin one deviceId and reuse it forever. Our token file
    normally carries it, but an older/partial file may not.
    """
    harness = Harness(root)
    harness.write_token(
        {
            "userId": "user-1",
            "passToken": "pass-1",
            "micoapi": ["old-ssecurity", "expired-service"],
        }
    )
    harness.write_auth({"userId": "user-1", "passToken": "pass-1"})
    harness.poll_script = [([], False), ([], False), ([], False)]

    async def bounded_sleep(delay):
        harness.sleeps.append(delay)
        if len(harness.sleeps) >= 2:
            harness.bridge_state["running"] = False

    harness.namespace["asyncio"].sleep = bounded_sleep
    harness.run()

    used = [call["device_id"] for call in harness.verified_calls]
    assert used, "the missing deviceId case must still exchange a token"
    assert all(used), f"the exchange must never send an empty deviceId, got {used}"
    assert len(set(used)) == 1, (
        f"every exchange must reuse one deviceId, got {sorted(set(used))}"
    )
    saved = harness.read_token()
    assert saved.get("deviceId") == used[0], (
        f"the deviceId must be persisted for the next restart, got {saved.get('deviceId')!r}"
    )
    return "a missing deviceId is generated once and then reused"


def scenario_every_xiaoai_device_is_polled(root: Path):
    """issue #15: with several speakers, every one of them must be listened to."""
    harness = Harness(root)
    harness.cfg["xiaomi"]["device_did"] = ""
    FakeMiNAService.devices = [
        {"deviceID": "speaker-1", "hardware": "L05C", "name": "客厅"},
        {"deviceID": "speaker-2", "hardware": "L05C", "name": "卧室"},
    ]
    harness.write_token(
        {
            "userId": "user-1",
            "passToken": "pass-1",
            "deviceId": "account-device",
            "micoapi": ["ssecurity-value", "valid-service"],
        }
    )
    harness.write_auth(
        {"userId": "user-1", "passToken": "pass-1", "deviceId": "account-device"}
    )
    harness.poll_script = [([], False)] * 8

    async def bounded_sleep(delay):
        harness.sleeps.append(delay)
        if len(harness.sleeps) >= 6:
            harness.bridge_state["running"] = False

    harness.namespace["asyncio"].sleep = bounded_sleep
    try:
        harness.run()
    finally:
        FakeMiNAService.devices = None

    polled = harness.polled_devices
    assert "speaker-1" in polled, f"the first speaker must be polled, got {polled}"
    assert "speaker-2" in polled, (
        f"issue #15: the second speaker must be polled too, got {polled}"
    )
    return "every xiaoai device is polled, not just devices[0]"


def scenario_poll_target_filtering(root: Path):
    """device_ids narrows the set; device_did still means "only this one"."""
    harness = Harness(root)
    targets = harness.namespace["_poll_targets"]
    devices = [{"deviceID": "a", "hardware": "L05C"}, {"deviceID": "b", "did": "2"}]

    ids = lambda cfg, devs=devices: [item["deviceID"] for item in targets(cfg, devs)]
    assert ids({}) == ["a", "b"], f"default must be every device, got {ids({})}"
    assert ids({"device_ids": ["b"]}) == ["b"], "device_ids must act as an allow-list"
    assert ids({"device_ids": "a, b"}) == ["a", "b"], "a comma string must be accepted"
    assert ids({"device_did": "a"}) == ["a"], "device_did must still mean one device"
    assert ids({"device_did": "2"}) == ["b"], "device_did may also match the miot did"
    assert ids({"device_ids": ["zz"]}) == [], (
        "an explicit allow-list matching nothing must poll nothing, never an unselected device"
    )
    assert targets({"device_did": "zz"}, []) == [
        {"deviceID": "zz", "hardware": ""}
    ], "an empty device list must fall back to the configured speaker"
    assert targets({}, []) == [], "no devices and no config must poll nothing"
    return "poll targets honour device_ids / device_did / fallbacks"


def scenario_scope_limits_polling(root: Path):
    """A saved allow-list must decide which speakers are actually polled."""
    harness = Harness(root)
    harness.cfg["xiaomi"]["device_did"] = ""
    harness.cfg["xiaomi"]["device_ids"] = ["speaker-1", "speaker-3"]
    FakeMiNAService.devices = [
        {"deviceID": "speaker-1", "hardware": "L05C", "name": "客厅"},
        {"deviceID": "speaker-2", "hardware": "L05C", "name": "卧室"},
        {"deviceID": "speaker-3", "hardware": "L06A", "name": "书房"},
    ]
    harness.write_token(
        {
            "userId": "user-1",
            "passToken": "pass-1",
            "deviceId": "account-device",
            "micoapi": ["ssecurity-value", "valid-service"],
        }
    )
    harness.write_auth(
        {"userId": "user-1", "passToken": "pass-1", "deviceId": "account-device"}
    )
    harness.poll_script = [([], False)] * 8

    async def bounded_sleep(delay):
        harness.sleeps.append(delay)
        if len(harness.sleeps) >= 6:
            harness.bridge_state["running"] = False

    harness.namespace["asyncio"].sleep = bounded_sleep
    try:
        harness.run()
    finally:
        FakeMiNAService.devices = None

    polled = set(harness.polled_devices)
    assert "speaker-1" in polled, f"the first selected speaker must be polled, got {polled}"
    assert "speaker-3" in polled, f"the second selected speaker must be polled, got {polled}"
    assert "speaker-2" not in polled, (
        f"an unselected speaker must never be polled, got {polled}"
    )
    return "only the speakers in the saved allow-list are polled"


def scenario_scope_unmatched_polls_nothing(root: Path):
    """An allow-list that matches nothing must wait, not fall back to devices[0]."""
    harness = Harness(root)
    harness.cfg["xiaomi"]["device_did"] = ""
    harness.cfg["xiaomi"]["device_ids"] = ["gone-1", "gone-2"]
    FakeMiNAService.devices = [
        {"deviceID": "speaker-1", "hardware": "L05C", "name": "客厅"},
    ]
    harness.write_token(
        {
            "userId": "user-1",
            "passToken": "pass-1",
            "deviceId": "account-device",
            "micoapi": ["ssecurity-value", "valid-service"],
        }
    )
    harness.write_auth(
        {"userId": "user-1", "passToken": "pass-1", "deviceId": "account-device"}
    )
    harness.poll_script = [([], False)] * 8

    async def bounded_sleep(delay):
        harness.sleeps.append(delay)
        if len(harness.sleeps) >= 4:
            harness.bridge_state["running"] = False

    harness.namespace["asyncio"].sleep = bounded_sleep
    try:
        harness.run()
    finally:
        FakeMiNAService.devices = None

    assert harness.polled_devices == [], (
        f"an unmatched allow-list must poll nothing, got {harness.polled_devices}"
    )
    assert harness.log.has("一台都没发现"), harness.log.messages
    return "an unmatched allow-list waits for the selected speakers"


def scenario_save_credentials_never_blank(root: Path):
    harness = Harness(root)
    harness.write_token(
        {
            "userId": "user-1",
            "passToken": "pass-1",
            "deviceId": "account-device",
            "micoapi": ["old-ssecurity", "old-service"],
        }
    )
    harness.write_auth(
        {"userId": "user-1", "passToken": "pass-1", "deviceId": "account-device"}
    )
    save = harness.namespace["_save_auth_and_token"]

    class IncompleteAccount:
        def __init__(self, token):
            self.token = token

    save(IncompleteAccount({"deviceId": "other-device"}))
    assert harness.read_token()["passToken"] == "pass-1", "incomplete token must not blank passToken"
    assert harness.read_token()["micoapi"][1] == "old-service"

    save(IncompleteAccount({"userId": "user-2", "passToken": "pass-2"}))
    updated = harness.read_token()
    assert updated["userId"] == "user-1", "incomplete account switch must keep previous login"
    assert updated["micoapi"][1] == "old-service", "do not mix old service token with new user"
    save(IncompleteAccount({"userId": "user-3"}))
    assert harness.read_token() == updated, "new identity without a valid service token must not be saved"
    save(IncompleteAccount({"userId": "user-2", "passToken": "pass-2",
                            "micoapi": ["sec-2", "svc-2"]}))
    assert harness.read_token()["userId"] == "user-2"
    assert harness.read_token()["micoapi"][1] == "svc-2"
    return "_save_auth_and_token switches account only with complete service credentials"


def scenario_post_config_credentials(root: Path):
    harness = Harness(root)
    harness.write_token(
        {"userId": "user-1", "passToken": "pass-1", "deviceId": "d", "micoapi": ["s", "t"]}
    )
    harness.write_auth({"userId": "user-1", "passToken": "pass-1", "deviceId": "d"})
    harness.cfg["xiaomi"]["cookie_text"] = "userId=user-1; passToken=pass-1"
    harness.cfg["xiaomi"]["password"] = "secret-password"
    post_config = harness.namespace["post_config"]

    # 1. A page loaded before QR login still shows an empty cookie field.
    stale = {
        "xiaomi": {"username": "", "password": MASKED, "cookie_text": ""},
        "homeassistant": {"url": "http://homeassistant:80", "token": MASKED},
        "openai": {"api_key": ""},
    }
    result = real_asyncio.run(post_config(SimpleNamespace(config=stale)))
    assert result["ok"] is True
    assert harness.cfg["xiaomi"]["cookie_text"] == "userId=user-1; passToken=pass-1"
    assert harness.cfg["xiaomi"]["password"] == "secret-password"
    assert harness.cfg["homeassistant"]["token"] == "ha-secret"

    # 2. The masked value coming back from GET /api/config must be kept too.
    masked = {
        "xiaomi": {"username": "user-1", "password": MASKED, "cookie_text": MASKED},
        "homeassistant": {"url": "http://homeassistant:80", "token": MASKED},
        "openai": {"api_key": ""},
    }
    real_asyncio.run(post_config(SimpleNamespace(config=masked)))
    assert harness.cfg["xiaomi"]["cookie_text"] == "userId=user-1; passToken=pass-1"

    # 3. An explicit clear_login removes both the files and the stored cookie.
    clearing = {
        "xiaomi": {"username": "", "password": MASKED, "cookie_text": "", "clear_login": True},
        "homeassistant": {"url": "http://homeassistant:80", "token": MASKED},
        "openai": {"api_key": ""},
    }
    real_asyncio.run(post_config(SimpleNamespace(config=clearing)))
    assert harness.cfg["xiaomi"]["cookie_text"] == ""
    assert not harness.token_path.exists(), "explicit clear must delete .mi.token"
    assert not harness.auth_path.exists(), "explicit clear must delete auth.json"
    assert "clear_login" not in (harness.cfg.get("xiaomi") or {}), (
        "the transient clear_login flag must never be persisted into config.yaml"
    )
    return "post_config keeps secrets unless the user explicitly clears them"


def scenario_background_otp_must_not_count_as_login(root: Path):
    harness = Harness(root)
    harness.cfg["xiaomi"]["username"] = "someone"
    harness.cfg["xiaomi"]["password"] = "test-password"
    harness.login_needs_otp = True
    harness.run()
    assert harness.otp_cleanups == 1, "background login requiring OTP must close pending verification"
    assert not harness.otp_pending, "pending OTP state may not survive a failed background login"
    assert not harness.bridge_state["connected"], "need_otp is not verified login"
    assert not harness.auth_path.exists() and not harness.token_path.exists(), (
        "OTP pending may not persist a new credential set"
    )
    return "background startup requiring OTP refuses false success and closes its session"


def scenario_atomic_timestamp_and_corruption(root: Path):
    harness = Harness(root)
    harness.write_token({
        "userId": "user-1", "passToken": "pass-1", "deviceId": "account-device",
        "micoapi": ["ssecurity-value", "valid-service"],
    })
    harness.write_auth({
        "userId": "user-1", "passToken": "pass-1", "deviceId": "account-device",
    })
    path = harness.cfg_path.parent / "last_timestamp.json"
    writer = harness.namespace["_write_last_timestamp_atomic"]
    writer(path, {"speaker-1": 123456})
    assert json.loads(path.read_text(encoding="utf-8")) == {"speaker-1": 123456}
    writer(path, {"speaker-1": 123457, "speaker-2": 42})
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "speaker-1": 123457, "speaker-2": 42
    }
    assert not list(path.parent.glob(".last-timestamp-*")), "atomic writer left temporary files"
    if __import__("os").name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    path.write_text("{BROKEN", encoding="utf-8")
    harness.run()
    assert harness.bridge_state["running"] is False
    assert "损坏" in str(harness.bridge_state["error"])
    assert path.read_text(encoding="utf-8") == "{BROKEN", (
        "corrupted timestamps may not be silently reset, or old commands could replay"
    )
    return "timestamps use atomic private writes and fail closed on corruption"


def scenario_mixed_credential_pair_is_rejected(root: Path):
    harness = Harness(root)
    harness.write_auth({
        "userId": "account-A", "passToken": "pass-A", "deviceId": "a",
    })
    harness.write_token({
        "userId": "account-B", "passToken": "pass-B", "deviceId": "b",
        "micoapi": ["sec-B", "svc-B"],
    })
    original_auth = harness.auth_path.read_bytes()
    original_token = harness.token_path.read_bytes()
    harness.run()
    assert harness.bridge_state["running"] is False
    assert harness.bridge_state["connected"] is False
    assert "不一致" in str(harness.bridge_state["error"])
    assert harness.auth_path.read_bytes() == original_auth
    assert harness.token_path.read_bytes() == original_token
    return "startup refuses mixed or interrupted Xiaomi credential identities"


def scenario_multi_entity_rules():
    intent_node = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "IntentParser"
    )
    namespace = {"re": re}
    exec(compile(ast.Module(body=[intent_node], type_ignores=[]), str(source_path), "exec"), namespace)
    multi = ["switch.one", "switch.two", "switch.three", "switch.four"]
    parsed = namespace["IntentParser"]([
        {
            "pattern": "打开客厅灯",
            "action": {"domain": "switch", "service": "turn_on", "entity_id": multi},
        },
    ]).parse("打开客厅灯")
    assert parsed["entity_id"] == multi, parsed
    single = namespace["IntentParser"]([
        {
            "pattern": "打开台灯",
            "action": {"domain": "switch", "service": "turn_on", "entity_id": "switch.desk"},
        },
    ]).parse("打开台灯")
    assert single["entity_id"] == "switch.desk", "single entity string must stay unchanged"
    return "multi-entity rules keep arrays, single entity stays a string"


def main():
    install_miservice_stubs()
    results = []
    scenarios = [
        scenario_normal_start,
        scenario_startup_refresh,
        scenario_qr_only_start,
        scenario_runtime_refresh_recovers,
        scenario_scheduled_rule_never_executes_now,
        scenario_schedule_ai_fallback_keeps_time,
        scenario_renewal_updates_device_scope,
        scenario_unsaved_renewal_retries_persistence,
        scenario_proactive_renewal_keeps_session_alive,
        scenario_renewal_failure_stays_quiet,
        scenario_device_id_stays_stable,
        scenario_every_xiaoai_device_is_polled,
        scenario_poll_target_filtering,
        scenario_scope_limits_polling,
        scenario_scope_unmatched_polls_nothing,
        scenario_runtime_refresh_failure,
        scenario_runtime_refresh_rejected,
        scenario_save_credentials_never_blank,
        scenario_post_config_credentials,
        scenario_background_otp_must_not_count_as_login,
        scenario_atomic_timestamp_and_corruption,
        scenario_mixed_credential_pair_is_rejected,
    ]
    for scenario in scenarios:
        with tempfile.TemporaryDirectory() as temp_dir:
            FakeHAClient.calls.clear()
            FakeMiNAService.created.clear()
            FakeMiNAService.said.clear()
            FakeAccount.reject_service_login = False
            # Startup-only scenarios stop the poll loop as soon as login is done.
            FakeHAClient.stop_after_connect = scenario in (
                scenario_normal_start,
                scenario_startup_refresh,
                scenario_qr_only_start,
            )
            results.append(scenario(Path(temp_dir)))
    results.append(scenario_multi_entity_rules())
    for line in results:
        print(f"  ok  {line}")
    print(f"startup patch tests passed ({len(results)} scenarios)")


if __name__ == "__main__":
    main()
