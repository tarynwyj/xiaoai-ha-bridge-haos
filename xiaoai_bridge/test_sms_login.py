"""Regression tests for the SMS verification login flow (1.0.11).

Covers the defects found in 1.0.10:

* the frontend never called ``POST /api/test/xiaomi/verify`` (the endpoint was dead);
* a second "测试连接" click closed ``_pending_session`` and lost the verification state;
* the STS branch overwrote ``pending`` with CookieJar data (passToken/ssecurity lost);
* an incomplete credential set could still write a corrupted ``cookie_text``;
* the whole password + SMS + STS path was untested.

Everything runs against the *patched* bridge.py with fake aiohttp/miservice objects:
no network, no real Xiaomi account. The real-world Xiaomi behaviour still has to be
verified on a device (see the report).

Usage: python test_sms_login.py [/app/bridge.py] [--html /app/web/index.html]
"""

import ast
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import types
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

source_path = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/bridge.py")
html_path = Path("/app/web/index.html")
if "--html" in sys.argv:
    html_path = Path(sys.argv[sys.argv.index("--html") + 1])
source = source_path.read_text(encoding="utf-8")
tree = ast.parse(source)

checks = 0
failures = []
results = []


def check(condition, message):
    global checks
    checks += 1
    if not condition:
        failures.append(message)
    return bool(condition)


def ok(headline):
    results.append(headline)


# ───────────────────────────── fake aiohttp / miservice ─────────────────────────────
class FakeResponse:
    def __init__(self, status=200, headers=None):
        self.status = status
        self.headers = headers or {}


class FakeJar:
    """Minimal aiohttp CookieJar: filter_cookies returns whatever was injected."""

    def __init__(self):
        self.cookie = SimpleCookie()

    def set(self, text):
        self.cookie.load(text)

    def filter_cookies(self, url):
        host = getattr(url, "host", "") or ""
        if host.endswith("api2.mina.mi.com") or host.endswith("mina.mi.com"):
            return self.cookie
        return SimpleCookie()

    def cookies(self):
        return {k: m.value for k, m in self.cookie.items()}


class _GetContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, responses=None, cookie_text="", name="session"):
        self.responses = list(responses or [])
        self.jar = FakeJar()
        if cookie_text:
            self.jar.set(cookie_text)
        self.calls = []
        self.closed = False
        self.name = name

    @property
    def cookie_jar(self):
        return self.jar

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.responses:
            return _GetContext(self.responses.pop(0))
        return _GetContext(FakeResponse(200))

    async def close(self):
        self.closed = True


CURRENT = {
    "login_script": [],
    "devices": [{"deviceID": "speaker-1", "name": "书房音箱"}],
    "device_error": None,
    "service_token": "service-token-from-login",
    "new_session": None,
    "sessions": [],
}


class FakeMiAccount:
    def __init__(self, session, username, password, token_store=None):
        self.session = session
        self.username = username
        self.password = password
        self.token_store = token_store
        self.token = {}
        self.calls = []

    async def _serviceLogin(self, uri, data=None):
        self.calls.append((uri, data))
        if not CURRENT["login_script"]:
            raise RuntimeError("no scripted login response")
        return CURRENT["login_script"].pop(0)

    async def _securityTokenService(self, location, nonce, ssecurity):
        CURRENT.setdefault("security_calls", []).append((location, nonce, ssecurity))
        return CURRENT["service_token"]


class FakeMiNAService:
    def __init__(self, account):
        self.account = account

    async def device_list(self):
        if CURRENT["device_error"]:
            raise CURRENT["device_error"]
        return list(CURRENT["devices"])


miservice_mod = types.ModuleType("miservice")
miaccount_mod = types.ModuleType("miservice.miaccount")
miservice_mod.MiAccount = FakeMiAccount
miservice_mod.MiNAService = FakeMiNAService
miaccount_mod.get_random = lambda n=16: "RANDOMDEVICEID"
_saved_modules = {
    name: sys.modules.get(name) for name in ("miservice", "miservice.miaccount")
}
sys.modules["miservice"] = miservice_mod
sys.modules["miservice.miaccount"] = miaccount_mod

aiohttp_mod = types.ModuleType("aiohttp")
aiohttp_mod.ClientTimeout = lambda **kwargs: SimpleNamespace(**kwargs)


def _new_client_session(*args, **kwargs):
    session = FakeSession(name="client-%d" % len(CURRENT["sessions"]))
    CURRENT["sessions"].append(session)
    CURRENT["new_session"] = session
    return session


aiohttp_mod.ClientSession = _new_client_session


class StubApp:
    def __init__(self):
        self.routes = {}

    def post(self, path):
        def register(func):
            self.routes[path] = func
            return func

        return register

    def get(self, path):
        return self.post(path)


class Log:
    def __init__(self):
        self.messages = []

    def _record(self, level, message, args):
        self.messages.append((level, message % args if args else message))

    def debug(self, message, *args):
        self._record("debug", message, args)

    def info(self, message, *args):
        self._record("info", message, args)

    def warning(self, message, *args):
        self._record("warning", message, args)

    def error(self, message, *args):
        self._record("error", message, args)

    def exception(self, message, *args):
        self._record("exception", message, args)

    def joined(self):
        return "\n".join(text for _, text in self.messages)


FUNCTIONS = (
    "_normalize_verify_url",
    "_validate_sts_url",
    "_credentials_from_pending",
    "_missing_credentials",
    "_pending_age_seconds",
    "_pending_is_fresh",
    "_remember_pending_verification",
    "_clear_pending_login",
    "_close_pending_session",
    "_sms_lock_obj",
    "_redact_url",
    "_mask_secret_text",
    "_retry_login_for_sms",
    "_exchange_sts_service_token",
    "_probe_devices_with_token",
    "_persist_sms_credentials",
    "_finish_sms_verification",
    "_finish_cookie_login",
    "xiaomi_verify",
    "test_xiaomi",
)

CONSTANTS = ("SMS_PENDING_TTL", "SMS_STS_HOST", "SMS_STS_PATH_PREFIX",
             "SMS_VERIFY_URL_HOSTS", "_pending_created_at", "_pending_verify_url",
             "_sms_verify_lock")


class Harness:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="sms-login-"))
        self.config_path = self.root / "config" / "config.yaml"
        self.auth_path = self.root / "config" / "auth.json"
        self.token_path = self.root / "config" / ".mi.token"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg = {
            "xiaomi": {"username": "", "password": "", "cookie_text": "",
                       "device_did": "", "hardware": "L05C"},
            "homeassistant": {"url": "http://homeassistant:80", "token": "ha-token"},
            "intent_rules": [],
        }
        self.log = Log()
        self.app = StubApp()
        self.ns = {}
        self._build()

    def load_config(self):
        return json.loads(json.dumps(self.cfg))

    def save_config(self, cfg):
        self.cfg = json.loads(json.dumps(cfg))

    def _build(self):
        ns = {
            "aiohttp": aiohttp_mod,
            "app": self.app,
            "asyncio": asyncio,
            "json": json,
            "os": __import__("os"),
            "re": __import__("re"),
            "time": __import__("time"),
            "log": self.log,
            "CONFIG_PATH": self.config_path,
            "AUTH_PATH": self.auth_path,
            "TOKEN_PATH": self.token_path,
            "AUTH_TOKEN_PATH": self.root / "config" / ".access_token",
            "load_config": self.load_config,
            "save_config": self.save_config,
            "_get_cookie": lambda cfg: None,
            "_save_auth_and_token": lambda account: None,
            "_read_saved_credential": lambda path: {},
            "_load_saved_xiaomi_identity": lambda: {},
            "_refresh_micoapi_token": None,
            "_ERROR_HINTS": {},
            "_pending_login": {},
            "_pending_session": None,
            "_pending_created_at": 0.0,
            "_pending_verify_url": "",
            "_sms_verify_lock": None,
            "SMS_PENDING_TTL": 900,
            "SMS_STS_HOST": "api2.mina.mi.com",
            "SMS_STS_PATH_PREFIX": "/sts",
            "SMS_VERIFY_URL_HOSTS": ("account.xiaomi.com", "api2.mina.mi.com"),
            "bridge_state": {},
            "HTMLResponse": object,
            "JSONResponse": object,
            "BaseModel": object,
            "Request": object,
            "TestXiaomiRequest": object,
            "XiaomiVerifyRequest": object,
            "MiAccount": FakeMiAccount,
            "MiNAService": FakeMiNAService,
            "get_random": lambda n=16: "RANDOMDEVICEID",
        }
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name.isupper() and name not in ns:
                    try:
                        ns[name] = ast.literal_eval(node.value)
                    except (ValueError, SyntaxError):
                        # e.g. SMS_SECRET_PATTERN = re.compile(...)
                        try:
                            exec(compile(ast.Module(body=[node], type_ignores=[]),
                                         str(source_path), "exec"), ns)
                        except Exception:
                            pass
        self.ns = ns
        for class_name in ("SmsVerifyError",):
            node = next(
                (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name),
                None,
            )
            if node is None:
                failures.append(f"patched bridge.py is missing class {class_name}")
                continue
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(source_path), "exec"), ns)
        for name in FUNCTIONS:
            node = next(
                (n for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name),
                None,
            )
            if node is None:
                failures.append(f"patched bridge.py is missing {name}()")
                continue
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(source_path), "exec"), ns)

    # -- state helpers -------------------------------------------------------
    def reset_pending(self):
        self.ns["_pending_login"].clear()
        self.ns["_pending_session"] = None
        self.ns["_pending_created_at"] = 0.0
        self.ns["_pending_verify_url"] = ""

    def start_pending(self, password="pw", user="login-user", pass_token="login-pass",
                      ssecurity="sec", session_cookies=""):
        session = FakeSession(name="pending", cookie_text=session_cookies)
        self.ns["_pending_login"].update({
            "username": "13800000000",
            "password": password,
            "userId": user,
            "passToken": pass_token,
            "ssecurity": ssecurity,
            "nonce": "nonce",
            "location": "location",
            "deviceId": "pending-device",
        })
        self.ns["_pending_session"] = session
        self.ns["_remember_pending_verification"]("https://account.xiaomi.com/pass/verify")
        return session

    def snapshot_data(self):
        snapshot = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_file():
                snapshot[str(path.relative_to(self.root))] = path.read_bytes()
        return snapshot

    def files(self):
        return {name: data for name, data in self.snapshot_data().items()}

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def run(coroutine):
    return asyncio.run(coroutine)


def endpoint_call(h, text):
    return run(h.ns["xiaomi_verify"](SimpleNamespace(cookie_text=text)))


def login_call(h, username="13800000000", password="pw", cookie_text="", use_saved=False):
    return run(h.ns["test_xiaomi"](
        SimpleNamespace(username=username, password=password,
                        cookie_text=cookie_text, use_saved=use_saved)
    ))


# ───────────────────────────── 1. trigger securityStatus & 16 ─────────────────────────────
def scenario_security_status_triggers_verification():
    h = Harness()
    try:
        CURRENT["login_script"] = [{
            "code": 0,
            "securityStatus": 16,
            "notificationUrl": "https://account.xiaomi.com/pass/verify?a=1",
            "userId": "login-user",
            "passToken": "login-pass",
            "ssecurity": "sec",
            "nonce": "nonce",
            "location": "location",
        }]
        data = login_call(h)
        check(data.get("need_verify") is True, f"securityStatus 16 must ask for verification, got {data}")
        check(data.get("verify_url") == "https://account.xiaomi.com/pass/verify?a=1",
              f"verify url mismatch: {data.get('verify_url')!r}")
        check(data.get("pending_seconds_left") == 900,
              f"pending TTL must be advertised, got {data.get('pending_seconds_left')}")
        check(h.ns["_pending_login"].get("userId") == "login-user",
              "pending login must keep the userId from the login response")
        check(h.ns["_pending_login"].get("passToken") == "login-pass",
              "pending login must keep passToken")
        check(h.ns["_pending_session"] is not None, "pending session must be stored")
        check(h.ns["_pending_verify_url"].startswith("https://account.xiaomi.com/pass/verify"),
              "pending verify url must be remembered")
        ok("securityStatus & 16 -> need_verify with a remembered pending session")
    finally:
        h.cleanup()


# ─────────────────── 2. URL normalization (legal forms) ───────────────────
def scenario_verify_url_normalization():
    h = Harness()
    try:
        normalize = h.ns["_normalize_verify_url"]
        cases = [
            ("https://account.xiaomi.com/pass/verify?a=1", "https://account.xiaomi.com/pass/verify?a=1"),
            ("https//account.xiaomi.com/pass/x", "https://account.xiaomi.com/pass/x"),
            ("//account.xiaomi.com/pass/x", "https://account.xiaomi.com/pass/x"),
            ("/pass/verify?a=2", "https://account.xiaomi.com/pass/verify?a=2"),
            ("pass/verify", "https://account.xiaomi.com/pass/verify"),
            ("", "https://account.xiaomi.com"),
            ("http://account.xiaomi.com/pass/x", "https://account.xiaomi.com"),
            ("https://evil.example/pass/x", "https://account.xiaomi.com"),
            ("https://account.xiaomi.com:8443/pass/x", "https://account.xiaomi.com"),
            ("https://user:pw@account.xiaomi.com/pass/x", "https://account.xiaomi.com"),
            ("https://api2.mina.mi.com/sts?x=1", "https://api2.mina.mi.com/sts?x=1"),
        ]
        for raw, expected in cases:
            got = normalize(raw)
            check(got == expected, f"normalize({raw!r}) -> {got!r}, expected {expected!r}")
        ok("verification links normalize safely (11 forms, bad hosts fall back)")
    finally:
        h.cleanup()


# ─────────────── 3. invalid STS URLs, cross-host, IP, non-HTTPS ───────────────
def scenario_sts_url_validation():
    h = Harness()
    try:
        validate = h.ns["_validate_sts_url"]
        accepted = [
            "https://api2.mina.mi.com/sts?sign=abc&d=1",
            "https://api2.mina.mi.com/sts/stsAuth?x=1",
            "https://api2.mina.mi.com:443/sts?x=1",
        ]
        for raw in accepted:
            try:
                normalized = validate(raw)
            except ValueError as exc:
                check(False, f"{raw!r} must be accepted, got {exc}")
                continue
            parts = urlsplit(normalized)
            check(parts.hostname == "api2.mina.mi.com", f"host must be kept: {normalized}")
            check(parts.scheme == "https", f"scheme must be https: {normalized}")
            check("#" not in normalized, "fragment must be dropped")

        rejected = [
            "",
            "http://api2.mina.mi.com/sts?x=1",
            "https://api2.mina.mi.com.evil.com/sts?x=1",
            "https://api2.mina.mi.com@evil.com/sts?x=1",
            "https://evil.com/sts?x=1",
            "https://127.0.0.1/sts?x=1",
            "https://api2.mina.mi.com:8443/sts?x=1",
            "https://api2.mina.mi.com/other?x=1",
            "https://api2.mina.mi.com/sts?x=1\nhttps://evil.com",
            "api2.mina.mi.com/sts?x=1",
        ]
        for raw in rejected:
            try:
                validate(raw)
            except ValueError:
                continue
            check(False, f"{raw!r} must be rejected")
        ok("STS url validation: 3 accepted forms, 10 hostile forms rejected")
    finally:
        h.cleanup()


# ─────────────── 4. STS exchange success + credential persistence ───────────────
def scenario_sts_success_persists_credentials():
    h = Harness()
    try:
        session = h.start_pending()
        session.responses = [FakeResponse(200)]
        session.jar.set("serviceToken=service-token-from-sts")
        CURRENT["login_script"] = []
        CURRENT["devices"] = [{"deviceID": "speaker-1", "name": "书房音箱"}]
        CURRENT["device_error"] = None
        data = endpoint_call(h, "https://api2.mina.mi.com/sts?sign=sig&d=1")

        check(data.get("ok") is True, f"STS flow must succeed, got {data}")
        check(data.get("verified") is True, "a successful login must be marked verified")
        check(len(data.get("devices") or []) == 1, "device list must be returned")
        check(not session.calls or session.calls[0][1].get("allow_redirects") is False,
              "the STS request must not follow redirects")

        if not check(h.auth_path.exists() and h.token_path.exists(),
                     "STS success must persist auth.json and .mi.token"):
            return "STS success did not persist credentials"
        auth = json.loads(h.auth_path.read_text(encoding="utf-8"))
        token = json.loads(h.token_path.read_text(encoding="utf-8"))
        check(auth == {"passToken": "login-pass", "userId": "login-user",
                       "deviceId": "pending-device"},
              f"auth.json must hold the login identity, got {auth}")
        check(token.get("micoapi") == ["sec", "service-token-from-sts"],
              f".mi.token micoapi must be [ssecurity, serviceToken], got {token.get('micoapi')}")
        check(token.get("userId") == "login-user" and token.get("passToken") == "login-pass",
              "the token file must keep userId/passToken")
        cookie_text = h.cfg["xiaomi"]["cookie_text"]
        check("passToken=login-pass" in cookie_text and "serviceToken=service-token-from-sts" in cookie_text,
              f"cookie_text must be complete, got {cookie_text!r}")
        check("Set-Cookie" not in cookie_text, f"cookie_text must not contain header garbage: {cookie_text!r}")
        check(h.ns["_pending_login"] == {}, "pending state must be cleared after success")
        check(session.closed is True, "the pending session must be closed after success")
        ok("STS exchange persists a complete credential set and clears the pending state")
    finally:
        h.cleanup()


# ─────────────── 5. retry-login path wins and keeps passToken/ssecurity ───────────────
def scenario_retry_login_keeps_pass_token_and_ssecurity():
    h = Harness()
    try:
        # CookieJar deliberately carries DIFFERENT values: they must not win.
        session = h.start_pending(user="login-user", pass_token="login-pass", ssecurity="ssecurity-from-login",
                                  session_cookies="userId=jar-user; passToken=jar-pass; deviceId=jar-device")
        CURRENT["login_script"] = [{
            "code": 0,
            "ssecurity": "ssecurity-from-login",
            "location": "location",
            "nonce": "nonce",
            "userId": "login-user",
            "passToken": "login-pass",
        }]
        CURRENT["service_token"] = "service-token-from-retry"
        CURRENT["devices"] = [{"deviceID": "speaker-1", "name": "书房音箱"}]
        CURRENT["device_error"] = None
        data = endpoint_call(h, "https://api2.mina.mi.com/sts?sign=sig")

        check(data.get("ok") is True, f"retry-login flow must succeed, got {data}")
        check(session.calls == [], "the STS url must not be fetched when the retry already worked")
        if not check(h.token_path.exists(), "retry login must persist .mi.token"):
            return "retry login did not persist credentials"
        token = json.loads(h.token_path.read_text(encoding="utf-8"))
        check(token.get("passToken") == "login-pass",
              f"passToken must come from the login state, got {token.get('passToken')!r}")
        check(token.get("userId") == "login-user",
              f"userId must not be taken from the CookieJar, got {token.get('userId')!r}")
        check(token.get("micoapi", [""])[0] == "ssecurity-from-login",
              f"ssecurity must survive, got {token.get('micoapi')}")
        check(token.get("micoapi", ["", ""])[1] == "service-token-from-retry",
              f"serviceToken must come from the retry, got {token.get('micoapi')}")
        ok("retry login wins, CookieJar cannot overwrite passToken/ssecurity/userId")
    finally:
        h.cleanup()


# ─────────────── 6. failure paths never touch /data ───────────────
def scenario_failures_do_not_pollute_data():
    h = Harness()
    try:
        # a) STS answers with a redirect
        session = h.start_pending()
        session.responses = [FakeResponse(302, {"Location": "https://evil.example/x"})]
        CURRENT["login_script"] = []
        before = h.snapshot_data()
        data = endpoint_call(h, "https://api2.mina.mi.com/sts?sign=sig")
        check(data.get("ok") is False and data.get("retry") is False,
              f"a redirect must fail and not be retried blindly, got {data}")
        check("重定向" in data.get("msg", ""), f"redirect must be named in the message: {data.get('msg')}")
        check(h.snapshot_data() == before, "a redirect must not write any /data file")
        check(h.ns["_pending_session"] is session, "a redirect keeps the pending session for a retry")

        # b) 200 but no serviceToken
        session = h.start_pending()
        session.responses = [FakeResponse(200)]
        before = h.snapshot_data()
        data = endpoint_call(h, "https://api2.mina.mi.com/sts?sign=sig")
        check(data.get("ok") is False and data.get("retry") is True,
              f"missing serviceToken must be a retryable failure, got {data}")
        check("serviceToken" in data.get("msg", ""), f"message must name the cause: {data.get('msg')}")
        check(h.snapshot_data() == before, "missing serviceToken must not write any /data file")

        # c) pending state without passToken
        session = h.start_pending(pass_token="")
        session.responses = [FakeResponse(200)]
        session.jar.set("serviceToken=service-token-from-sts")
        before = h.snapshot_data()
        data = endpoint_call(h, "https://api2.mina.mi.com/sts?sign=sig")
        check(data.get("ok") is False, f"missing passToken must fail, got {data}")
        check("passToken" in data.get("msg", ""), f"message must name passToken: {data.get('msg')}")
        check(h.snapshot_data() == before, "missing passToken must not write any /data file")

        # d) device_list fails: credentials are valid but unusable
        session = h.start_pending()
        session.responses = [FakeResponse(200)]
        session.jar.set("serviceToken=service-token-from-sts")
        CURRENT["device_error"] = RuntimeError("mina 500")
        before = h.snapshot_data()
        data = endpoint_call(h, "https://api2.mina.mi.com/sts?sign=sig")
        check(data.get("ok") is False and data.get("retry") is True,
              f"device_list failure must be retryable and not reported as success, got {data}")
        check(h.snapshot_data() == before,
              "credentials must not be persisted before the device list succeeded")
        CURRENT["device_error"] = None

        # e) persistence refuses an incomplete credential set
        try:
            h.ns["_persist_sms_credentials"]({"userId": "u", "passToken": "p"})
            check(False, "persistence must refuse a set without serviceToken")
        except ValueError as exc:
            check("serviceToken" in str(exc), f"the error must name the missing key: {exc}")
        check(not h.auth_path.exists() and not h.token_path.exists(),
              "a refused persistence must not create credential files")
        ok("redirect / no-token / missing-passToken / device failure never touch /data")
    finally:
        h.cleanup()


# ─────────────── 7. session expiry + repeat requests + concurrency ───────────────
def scenario_pending_lifecycle_and_concurrency():
    h = Harness()
    try:
        # a) a second "测试连接" while a verification is pending must not kill it
        CURRENT["login_script"] = [{
            "code": 0, "securityStatus": 16,
            "notificationUrl": "https://account.xiaomi.com/pass/verify?a=1",
            "userId": "login-user", "passToken": "login-pass",
            "ssecurity": "sec", "nonce": "nonce", "location": "location",
        }]
        first = login_call(h)
        pending_session = h.ns["_pending_session"]
        check(first.get("need_verify") is True, "first login must ask for verification")
        second = login_call(h)
        check(second.get("need_verify") is True, "a repeat click must still report need_verify")
        check(second.get("verify_url") == first.get("verify_url"),
              "a repeat click must return the same verification link")
        check(h.ns["_pending_session"] is pending_session,
              "a repeat click must not replace the pending session")
        check(pending_session.closed is False,
              "a repeat click must not close the session that holds the verification state")

        # b) expiry: the session is closed and the state dropped, then a fresh login starts
        h.ns["_pending_created_at"] = 1.0
        CURRENT["login_script"] = [{
            "code": 0, "securityStatus": 16,
            "notificationUrl": "/pass/verify?b=2",
            "userId": "login-user", "passToken": "login-pass",
            "ssecurity": "sec", "nonce": "nonce", "location": "location",
        }]
        expired = login_call(h)
        check(pending_session.closed is True, "an expired pending session must be closed")
        check(h.ns["_pending_session"] is not pending_session,
              "an expired verification must be replaced by a new session")
        check(expired.get("verify_url") == "https://account.xiaomi.com/pass/verify?b=2",
              f"the new login must expose the new link, got {expired.get('verify_url')!r}")

        # c) endpoint with no pending state at all
        h.reset_pending()
        data = endpoint_call(h, "https://api2.mina.mi.com/sts?sign=sig")
        check(data.get("ok") is False and data.get("retry") is False,
              f"a stale callback must not be retried blindly, got {data}")

        # d) concurrent verification: the second one is rejected while the first runs
        h.reset_pending()
        h.start_pending()
        lock = h.ns["_sms_lock_obj"]()

        async def concurrency_probe():
            await lock.acquire()
            try:
                return await h.ns["xiaomi_verify"](
                    SimpleNamespace(cookie_text="https://api2.mina.mi.com/sts?sign=sig")
                )
            finally:
                lock.release()

        busy = run(concurrency_probe())
        check(busy.get("ok") is False and "处理中" in busy.get("msg", ""),
              f"a concurrent verification must be rejected, got {busy}")
        ok("pending lifecycle: repeat click keeps the session, expiry restarts it, concurrency guarded")
    finally:
        h.cleanup()


# ─────────────── 8. cookie login path still works (backwards compatible) ───────────────
def scenario_cookie_login_still_works():
    h = Harness()
    try:
        CURRENT["devices"] = [{"deviceID": "speaker-9", "name": "客厅音箱"}]
        CURRENT["device_error"] = None
        data = endpoint_call(
            h,
            "userId=u9; passToken=p9; serviceToken=svc9; deviceId=d9",
        )
        check(data.get("ok") is True, f"cookie login must keep working, got {data}")
        check(len(data.get("devices") or []) == 1, "cookie login must return the device list")
        if not check(h.token_path.exists(), "cookie login must persist .mi.token"):
            return "cookie login did not persist credentials"
        token = json.loads(h.token_path.read_text(encoding="utf-8"))
        check(token.get("micoapi") == ["", "svc9"], f"cookie login must persist the serviceToken: {token}")

        # an incomplete cookie is rejected without touching /data
        before = h.snapshot_data()
        bad = endpoint_call(h, "userId=u9; deviceId=d9")
        check(bad.get("ok") is False and "passToken" in bad.get("msg", ""),
              f"an incomplete cookie must be named, got {bad}")
        check(h.snapshot_data() == before, "an incomplete cookie must not write /data")
        ok("whole-cookie login still works; incomplete cookies are rejected without writes")
    finally:
        h.cleanup()


# ─────────────── 9. no secret ever reaches the log ───────────────
def scenario_logs_do_not_leak_secrets():
    h = Harness()
    try:
        session = h.start_pending()
        session.responses = [FakeResponse(200)]
        session.jar.set("serviceToken=super-secret-service-token")
        CURRENT["login_script"] = []
        endpoint_call(h, "https://api2.mina.mi.com/sts?sign=TOP-SECRET-SIGNATURE&d=1")
        log_text = h.log.joined()
        check("TOP-SECRET-SIGNATURE" not in log_text,
              f"the STS signature must never be logged: {log_text}")
        check("super-secret-service-token" not in log_text,
              f"the serviceToken must never be logged: {log_text}")
        check("login-pass" not in log_text, f"passToken must never be logged: {log_text}")
        check("api2.mina.mi.com/sts..." in log_text or "api2.mina.mi.com/sts" in log_text,
              "the redacted url should still identify the host/path for debugging")
        ok("logs keep host+path only: signature, serviceToken and passToken never appear")
    finally:
        h.cleanup()


# ─────────────── 10. frontend button really calls /verify ───────────────
def scenario_frontend_calls_verify():
    if not html_path.exists():
        print(f"SKIPPED frontend /verify wiring: no html at {html_path}")
        return None
    html = html_path.read_text(encoding="utf-8")
    start = html.find("async function completeSmsVerify(){")
    end = html.find("async function qrLogin(){", start)
    check(start >= 0, "the page must define completeSmsVerify()")
    check(end > start, "completeSmsVerify() must be placed before qrLogin()")
    if start < 0 or end <= start:
        return "frontend wiring check aborted"
    function_source = html[start:end]
    check("/api/test/xiaomi/verify" in function_source,
          "completeSmsVerify() must POST to /api/test/xiaomi/verify")
    check("mi-sts-url" in html and "mi-sts-submit" in html,
          "the verification card must expose the url input and the finish button")
    check('id="mi-sts-card"' in html and "display:none" in html,
          "the verification card must stay hidden until needed")

    harness = """
const calls=[];
const elements={
  'xiaomi-alert':{className:'',innerHTML:''},
  'mi-sts-status':{textContent:''},
  'mi-sts-url':{value:'https://api2.mina.mi.com/sts?sign=sig'},
  'mi-sts-submit':{disabled:false},
  'mi-sts-card':{style:{display:''}},
};
global.document={getElementById:(id)=>elements[id]||null};
global.showAlert=(a,cls,msg)=>{calls.push(['alert',cls,msg]);};
global.renderDeviceList=(d)=>{calls.push(['devices',d]);};
global.fetch=(url,init)=>{
  calls.push(['fetch',url,init&&init.method,init&&init.body]);
  return Promise.resolve({ok:true,json:()=>Promise.resolve(RESPONSE)});
};
(async()=>{
  await completeSmsVerify();
  console.log(JSON.stringify({calls,status:elements['mi-sts-status'].textContent,
    card:elements['mi-sts-card'].style.display,disabled:elements['mi-sts-submit'].disabled}));
})();
"""
    node = shutil.which("node")
    if not node:
        print("SKIPPED frontend execution: node is not available")
        return None
    results_payload = []
    for label, response in (
        ("success", {"ok": True, "msg": "短信验证完成", "devices": [{"deviceID": "d1"}]}),
        ("retry", {"ok": False, "retry": True, "msg": "缺少 passToken"}),
        ("expired", {"ok": False, "retry": False, "msg": "会话已失效"}),
    ):
        script = function_source + "\nconst RESPONSE=" + json.dumps(response) + ";\n" + harness
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False, encoding="utf-8") as handle:
            handle.write(script)
            script_path = handle.name
        try:
            completed = subprocess.run([node, script_path], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=60)
        finally:
            Path(script_path).unlink(missing_ok=True)
        check(completed.returncode == 0,
              f"node failed for {label}: {(completed.stderr or '').strip()[:300]}")
        if completed.returncode != 0:
            continue
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        results_payload.append((label, payload))
        fetches = [call for call in payload["calls"] if call[0] == "fetch"]
        check(len(fetches) == 1, f"{label}: exactly one fetch expected, got {fetches}")
        if fetches:
            check(fetches[0][1] == "/api/test/xiaomi/verify",
                  f"{label}: must call /api/test/xiaomi/verify, got {fetches[0][1]}")
            check(fetches[0][2] == "POST", f"{label}: must be a POST, got {fetches[0][2]}")
            check("api2.mina.mi.com/sts" in (fetches[0][3] or ""),
                  f"{label}: the pasted url must be sent, got {fetches[0][3]}")

    by_label = dict(results_payload)
    if "success" in by_label:
        payload = by_label["success"]
        check(payload["card"] == "none", "success must hide the verification card")
        check(any(call[0] == "devices" for call in payload["calls"]),
              "success must refresh the device list")
        check(payload["disabled"] is False, "the button must be re-enabled after the request")
    if "retry" in by_label:
        payload = by_label["retry"]
        check("可重试" in payload["status"], f"a retryable failure must keep the retry hint, got {payload['status']}")
    if "expired" in by_label:
        payload = by_label["expired"]
        check("测试连接" in payload["status"],
              f"an expired session must tell the user to restart, got {payload['status']}")
    ok("frontend executes completeSmsVerify(): POST /verify, url body, device refresh, retry hints")
    return "frontend wiring verified"


# ─────────────── 11. compatibility guards ───────────────
def scenario_compatibility():
    check("def _save_auth_and_token" in source, "the shared credential writer must stay in place")
    for route in ('@app.post("/api/qr/login")', '@app.post("/api/test/xiaomi")',
                  '@app.post("/api/test/xiaomi/verify")', '@app.get("/api/config")'):
        check(route in source, f"{route} must still be registered")
    check("_enforce_access_control" in source, "the HTTP auth middleware must still be installed")
    check("def _poll_auth_cookies" in source, "the 1.0.10 polling fix must stay in place")
    check("MiAccount(session, username, password, None)" in source
          or "MiAccount(s, username, password, None)" in source,
          "MiService must never be handed the real token file path")
    ok("compatibility: QR login, login/poll/auth routes and the 1.0.10 fixes stay in place")


def main():
    probe = Harness()
    probe.cleanup()
    if failures:
        print(f"sms login: {len(failures)} structural problem(s) found")
        for failure in failures:
            print(f"  FAIL {failure}")
        print("hint: this suite targets a bridge.py patched with 1.0.11 (patch_auth.py)")
        sys.exit(1)
    scenarios = [
        scenario_security_status_triggers_verification,
        scenario_verify_url_normalization,
        scenario_sts_url_validation,
        scenario_sts_success_persists_credentials,
        scenario_retry_login_keeps_pass_token_and_ssecurity,
        scenario_failures_do_not_pollute_data,
        scenario_pending_lifecycle_and_concurrency,
        scenario_cookie_login_still_works,
        scenario_logs_do_not_leak_secrets,
        scenario_compatibility,
    ]
    for scenario in scenarios:
        scenario()
    frontend = scenario_frontend_calls_verify()
    if frontend:
        results.append(frontend)

    if failures:
        print(f"sms login: {checks} checks, {len(failures)} FAILED")
        for failure in failures:
            print(f"  FAIL {failure}")
        sys.exit(1)
    for line in results:
        print(f"  ok  {line}")
    print(f"sms login tests passed ({checks} checks, {len(results)} scenarios)")


if __name__ == "__main__":
    main()
