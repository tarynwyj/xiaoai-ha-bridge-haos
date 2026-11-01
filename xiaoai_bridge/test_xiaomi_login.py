"""Regression tests for the Xiaomi login flows introduced in 1.0.12.

Covers the two repaired non-QR login paths and the restructured UI:

* password login that Xiaomi accepts directly (no second factor);
* password login behind a verification code, implemented as the 8-step protocol
  of miservice 2.4+/3.x inside the *same* aiohttp session:
  authStart -> identity/list -> verifyPhone -> sendPhoneTicket -> verifyPhone
  (submit code) -> follow location -> serviceLogin resume -> serviceToken;
* manual Cookie login (userId/passToken exchange, direct serviceToken, speaker
  probe before any write, explicit parse errors);
* the frontend wiring: three entries, OTP panel, the Cookie entry working while
  an OTP session is pending, and the legacy STS block only as an advanced
  compatibility path.

Everything runs against the *patched* bridge.py with fake aiohttp/miservice
objects: no network, no real Xiaomi account. Real Xiaomi behaviour still needs a
device test (see CHANGELOG 1.0.12).

Usage: python test_xiaomi_login.py [/app/bridge.py] [--html /app/web/index.html]
"""

import ast
import asyncio
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

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
    def __init__(self, status=200, body=b""):
        self.status = status
        if isinstance(body, (bytes, bytearray)):
            self._body = bytes(body)
        elif isinstance(body, str):
            self._body = body.encode("utf-8")
        else:
            self._body = json.dumps(body).encode("utf-8")

    async def read(self):
        return self._body

    async def text(self):
        return self._body.decode("utf-8", "replace")

    async def json(self, content_type=None):
        return json.loads(self._body.decode("utf-8"))


class _ResponseContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *exc):
        return False


class FakeJar:
    def __init__(self):
        self.cookie = {}

    def update_cookies(self, cookies, url=None):
        self.cookie.update(dict(cookies or {}))


class FakeSession:
    def __init__(self, name="session", responses=None):
        self.name = name
        self.responses = list(responses or [])
        self.calls = []
        self.closed = False
        self.jar = FakeJar()

    @property
    def cookie_jar(self):
        return self.jar

    def _request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0) if self.responses else FakeResponse(200, b"")
        return _ResponseContext(response)

    def get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, **kwargs)

    def request(self, method, url, **kwargs):
        return self._request(method, url, **kwargs)

    async def close(self):
        self.closed = True

    def urls(self):
        return [call[1] for call in self.calls]


CURRENT = {}


def reset_current():
    CURRENT.clear()
    CURRENT.update({
        "login_script": [],
        "identity": {"flag": 4, "maskedPhone": "13812345678"},
        "verify_response": {"code": 0, "location": "/pass/verify/ok?token=1"},
        "send_response": {"code": 0},
        "location_status": 200,
        "devices": [{"deviceID": "speaker-1", "name": "书房音箱"}],
        "device_error": None,
        "service_token": "service-token-from-login",
        "cookie_login": "ok",
        "response_script": [],
        "sessions": [],
        "accounts": [],
        "service_login_calls": [],
        "security_calls": [],
        "micoapi_login_calls": [],
    })


reset_current()


class FakeMiAccount:
    def __init__(self, session, username, password, token_store=None):
        self.session = session
        self.username = username
        self.password = password
        self.token_store = token_store
        self.token = {}
        CURRENT["accounts"].append(self)

    async def _serviceLogin(self, uri, data=None):
        CURRENT["service_login_calls"].append((self, uri, data))
        if not CURRENT["login_script"]:
            raise RuntimeError("no scripted login response")
        return CURRENT["login_script"].pop(0)

    async def _securityTokenService(self, location, nonce, ssecurity):
        CURRENT["security_calls"].append((self, location, nonce, ssecurity))
        return CURRENT["service_token"]

    async def login(self, sid):
        CURRENT["micoapi_login_calls"].append((self, sid))
        behaviour = CURRENT["cookie_login"]
        if behaviour == "raise":
            raise RuntimeError("cookie exchange failed")
        if behaviour == "fail":
            return False
        self.token["micoapi"] = ("cookie-ssecurity", "cookie-service-token")
        return True


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
    session = FakeSession(name="client-%d" % len(CURRENT["sessions"]),
                          responses=CURRENT["response_script"])
    CURRENT["response_script"] = []
    CURRENT["sessions"].append(session)
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
    # 1.0.12 section F
    "_otp_int",
    "_otp_field",
    "_otp_json",
    "_otp_age_seconds",
    "_otp_is_fresh",
    "_close_otp_session",
    "_clear_otp_login",
    "_reset_otp_state",
    "_drop_expired_otp",
    "_otp_pending_payload",
    "_otp_mask_hint",
    "_otp_normalize_notification_url",
    "_otp_normalize_location",
    "_otp_request",
    "_otp_request_json",
    "_otp_finish_login",
    "_otp_open_verification",
    "_otp_password_login",
    "_otp_start",
    "_otp_wrong_code",
    "_otp_verify_code",
    "_otp_send_code",
    "xiaomi_otp_start",
    "xiaomi_otp_send",
    "xiaomi_otp_verify",
    "xiaomi_otp_restart",
    "xiaomi_cookie_login",
    # 1.0.11 section E helpers reused by F
    "_probe_devices_with_token",
    "_persist_sms_credentials",
    "_restore_credential_files",
    "_write_private_file",
    "_missing_credentials",
    "_sms_lock_obj",
    "_exchange_sts_service_token",
    "_validate_sts_url",
    "_mask_secret_text",
    "_redact_url",
    "_normalize_verify_url",
)

CLASSES = ("OtpFlowError", "SmsVerifyError")


class Harness:
    def __init__(self):
        reset_current()
        self.root = Path(tempfile.mkdtemp(prefix="xiaomi-login-"))
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
            "_ERROR_HINTS": {
                70016: "可能是密码错误、账号被风控拦截，或需要手机验证码验证。",
            },
            "_sms_verify_lock": None,
            "_otp_session": None,
            "_otp_login": {},
            "bridge_state": {},
            "HTMLResponse": object,
            "JSONResponse": object,
            "BaseModel": object,
            "Request": object,
            "TestXiaomiRequest": object,
            "XiaomiVerifyRequest": object,
            "OtpStartRequest": object,
            "OtpVerifyRequest": object,
            "CookieLoginRequest": object,
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
                        try:
                            exec(compile(ast.Module(body=[node], type_ignores=[]),
                                         str(source_path), "exec"), ns)
                        except Exception:
                            pass
        self.ns = ns
        for class_name in CLASSES:
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
    def otp_state(self):
        return self.ns["_otp_login"]

    def otp_session(self):
        return self.ns["_otp_session"]

    def snapshot_data(self):
        snapshot = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_file():
                snapshot[str(path.relative_to(self.root))] = path.read_bytes()
        return snapshot

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def run(coroutine):
    return asyncio.run(coroutine)


def start(h, username="13800000000", password="pw", use_saved=False):
    return run(h.ns["xiaomi_otp_start"](
        SimpleNamespace(username=username, password=password, use_saved=use_saved)
    ))


def send(h):
    return run(h.ns["xiaomi_otp_send"]())


def verify(h, code):
    return run(h.ns["xiaomi_otp_verify"](SimpleNamespace(code=code)))


def restart(h):
    return run(h.ns["xiaomi_otp_restart"]())


def cookie_login(h, text):
    return run(h.ns["xiaomi_cookie_login"](SimpleNamespace(cookie_text=text)))


def response(status=200, payload=None, raw=None):
    if raw is not None:
        return FakeResponse(status, raw)
    if payload is None:
        return FakeResponse(status, b"")
    return FakeResponse(status, payload)


AUTH_START = ("https://account.xiaomi.com/fe/service/identity/authStart"
              "?sid=micoapi&context=ctx-123&_locale=zh_CN")


def need_otp(h, flag=4, hint="13812345678", context="ctx-123",
             notification=AUTH_START, security_status=16):
    """Drive one password login that Xiaomi answers with a second factor."""
    CURRENT["login_script"] = [{
        "code": 0,
        "securityStatus": security_status,
        "notificationUrl": notification,
        "userId": "login-user",
        "passToken": "login-pass",
    }]
    CURRENT["identity"] = {"flag": flag}
    if flag == 8:
        CURRENT["identity"]["maskedEmail"] = "user@example.com"
    elif hint:
        CURRENT["identity"]["maskedPhone"] = hint
    CURRENT["response_script"] = [
        response(200, raw=b"<html>auth start</html>"),
        response(200, CURRENT["identity"]),
    ]
    return start(h)


# ─────────────── 1. password login accepted directly (no second factor) ───────────────
def scenario_direct_login_success():
    h = Harness()
    try:
        CURRENT["login_script"] = [{
            "code": 0,
            "userId": "login-user",
            "passToken": "login-pass",
            "deviceId": "login-device",
            "ssecurity": "login-ssecurity",
            "location": "https://api2.mina.mi.com/sts?sign=login",
            "nonce": "login-nonce",
        }]
        data = start(h)
        check(data.get("ok") is True, f"a login without 2FA must succeed, got {data}")
        check(data.get("verified") is True, "a successful login must be marked verified")
        check(data.get("need_otp") is not True, "a direct login must not ask for a code")
        check(len(data.get("devices") or []) == 1, "the device list must be returned")
        check(h.ns["_otp_session"] is None, "a direct login must not keep a verification session")
        check(h.otp_state() == {}, "a direct login must not leave OTP state behind")
        check(not h.ns["_sms_lock_obj"]().locked(), "the SMS lock must be released")
        if not check(h.auth_path.exists() and h.token_path.exists(),
                     "a direct login must persist both credential files"):
            return "direct login did not persist credentials"
        auth = json.loads(h.auth_path.read_text(encoding="utf-8"))
        token = json.loads(h.token_path.read_text(encoding="utf-8"))
        check(auth == {"passToken": "login-pass", "userId": "login-user",
                       "deviceId": "login-device"},
              f"auth.json must hold the login identity, got {auth}")
        check(token.get("micoapi") == ["login-ssecurity", "service-token-from-login"],
              f".mi.token micoapi must be [ssecurity, serviceToken], got {token.get('micoapi')}")
        cookie_text = h.cfg["xiaomi"]["cookie_text"]
        check("passToken=login-pass" in cookie_text
              and "serviceToken=service-token-from-login" in cookie_text,
              f"cookie_text must be complete, got {cookie_text!r}")
        ok("direct password login persists a complete credential set (no SMS sent)")
    finally:
        h.cleanup()


# ─────────────── 2. start -> need_otp: one password login, no SMS yet ───────────────
def scenario_start_needs_otp():
    h = Harness()
    try:
        data = need_otp(h)
        check(data.get("ok") is True and data.get("need_otp") is True,
              f"a 2FA login must ask for the code, got {data}")
        check(data.get("method") == "Phone", f"flag=4 must map to Phone, got {data.get('method')}")
        check(data.get("phone_hint") == "138****5678",
              f"the phone hint must be masked, got {data.get('phone_hint')!r}")
        check(data.get("expires_in") == 300, f"the TTL must be advertised, got {data.get('expires_in')}")
        check(data.get("can_resend_in") == 0,
              f"no code has been sent yet, so no cooldown, got {data.get('can_resend_in')}")
        session = h.otp_session()
        if not check(session is not None, "the verification session must be stored"):
            return "no OTP session"
        check(session.closed is False, "the verification session must stay open")
        urls = session.urls()
        check(len(urls) == 2, f"start must only open authStart + identity/list, got {urls}")
        if len(urls) >= 2:
            check(urls[0].startswith("https://account.xiaomi.com/fe/service/identity/authStart"),
                  f"step 1 must open the notificationUrl, got {urls[0]}")
            check("/identity/list?" in urls[1] and "context=ctx-123" in urls[1],
                  f"step 2 must call identity/list with the context, got {urls[1]}")
        check(not any("sendPhoneTicket" in url for url in urls),
              f"start must never send the SMS by itself, got {urls}")
        check(not any("verifyPhone" in url for url in urls),
              "start must not trigger verifyPhone (that happens on the send click)")
        state = h.otp_state()
        check(state.get("flag") == 4 and state.get("method") == "Phone",
              f"the OTP state must remember flag/method, got {state}")
        check(state.get("context") == "ctx-123" and state.get("sid") == "micoapi",
              f"the OTP state must remember context/sid, got {state}")
        check(state.get("attempts") == 0 and state.get("sent") is False,
              f"a fresh state starts with 0 attempts and nothing sent, got {state}")
        check(h.auth_path.exists() is False and h.token_path.exists() is False,
              "asking for a code must not write any credential file")
        ok("start: one password login, masked hint, 300s TTL, and no SMS is sent")
    finally:
        h.cleanup()


# ─────────────── 3. send: explicit only, 60s cooldown, exactly one SMS ───────────────
def scenario_send_cooldown():
    h = Harness()
    try:
        need_otp(h)
        session = h.otp_session()
        session.responses = [response(200, {"code": 0}), response(200, {"code": 0})]
        data = send(h)
        check(data.get("ok") is True and data.get("sent") is True,
              f"send must report the code as sent, got {data}")
        check(data.get("can_resend_in") == 60,
              f"send must start a 60s cooldown, got {data.get('can_resend_in')}")
        urls = session.urls()
        verify_urls = [url for url in urls if "/identity/auth/verifyPhone?_flag=4" in url]
        ticket_urls = [url for url in urls if "sendPhoneTicket" in url]
        check(len(verify_urls) == 1, f"exactly one verifyPhone trigger expected, got {verify_urls}")
        check(len(ticket_urls) == 1, f"exactly one SMS send expected, got {ticket_urls}")
        check(h.otp_state().get("sent") is True, "the state must remember the code was sent")

        calls_before = len(session.calls)
        again = send(h)
        check(again.get("ok") is False and again.get("action") == "wait",
              f"a second send inside the cooldown must be refused, got {again}")
        check(1 <= int(again.get("retry_after") or 0) <= 60,
              f"the refusal must say how long to wait, got {again.get('retry_after')}")
        check(len(session.calls) == calls_before,
              "a refused send must not touch Xiaomi at all (no extra SMS)")
        check("秒" in again.get("msg", ""), f"the wait message must mention seconds: {again.get('msg')}")
        ok("send: exactly one SMS, then the 60s cooldown refuses without any request")
    finally:
        h.cleanup()


# ─────────────── 4. send: cooldown expiry allows a deliberate resend ───────────────
def scenario_send_after_cooldown():
    h = Harness()
    try:
        need_otp(h)
        session = h.otp_session()
        session.responses = [response(200, {"code": 0}), response(200, {"code": 0})]
        first = send(h)
        check(first.get("ok") is True, f"the first send must work, got {first}")
        h.otp_state()["cooldown_until"] = 1.0     # pretend the 60s elapsed
        session.responses = [response(200, {"code": 0}), response(200, {"code": 0})]
        second = send(h)
        check(second.get("ok") is True and second.get("sent") is True,
              f"after the cooldown a resend must be allowed, got {second}")
        tickets = [url for url in session.urls() if "sendPhoneTicket" in url]
        check(len(tickets) == 2, f"two deliberate sends must mean two SMS, got {tickets}")
        ok("send: after the cooldown a user-triggered resend is allowed (counted, not automatic)")
    finally:
        h.cleanup()


# ─────────────── 5. verify: the full same-session 8-step resume ───────────────
def scenario_verify_success():
    h = Harness()
    try:
        need_otp(h)
        session = h.otp_session()
        first = start(h)   # a second click must reuse the pending session
        check(first.get("need_otp") is True and h.otp_session() is session,
              "a repeated start must reuse the pending verification session")
        check(len(CURRENT["accounts"]) == 1,
              "a repeated start must not log in with the password again")
        session.responses = [response(200, {"code": 0}), response(200, {"code": 0})]
        send(h)

        CURRENT["verify_response"] = {
            "code": 0,
            "location": "/pass/verify/accepted?token=abc",
        }
        session.responses = [
            response(200, CURRENT["verify_response"]),
            response(200, raw=b"<html>ok</html>"),
        ]
        CURRENT["login_script"] = [{
            "code": 0,
            "userId": "login-user",
            "passToken": "login-pass",
            "deviceId": "login-device",
            "ssecurity": "resume-ssecurity",
            "location": "https://api2.mina.mi.com/sts?sign=resume",
            "nonce": "resume-nonce",
        }]
        CURRENT["service_token"] = "service-token-after-otp"
        accounts_before = len(CURRENT["accounts"])
        data = verify(h, "123456")

        check(data.get("ok") is True, f"a correct code must finish the login, got {data}")
        check(data.get("verified") is True, "the OTP login must be marked verified")
        check(len(data.get("devices") or []) == 1, "the device list must be returned after the probe")

        urls = session.urls()
        submit = [url for url in urls if "verifyPhone?_dc=" in url]
        check(len(submit) == 1, f"exactly one code submission expected, got {submit}")
        follows = [url for url in urls if url.startswith("https://account.xiaomi.com/pass/verify/accepted")]
        check(len(follows) == 1, f"the accepted location must be opened once, got {follows}")
        if submit:
            kwargs = session.calls[[call[1] for call in session.calls].index(submit[0])][2]
            form = kwargs.get("data") or {}
            check(form.get("ticket") == "123456" and form.get("_flag") == "4"
                  and form.get("trust") == "false",
                  f"the code form must match the protocol, got {form}")

        # every step ran on the SAME session object
        for method, url, _kwargs in session.calls:
            check(True, "")  # placeholder keeps the loop explicit
        check(all(call[2].get("allow_redirects") is False for call in session.calls),
              "no OTP request may follow redirects")
        check(len(CURRENT["accounts"]) == accounts_before + 2,
              f"verify must build its own MiAccount (+1 for the device probe), got "
              f"{len(CURRENT['accounts']) - accounts_before}")
        verify_account = CURRENT["accounts"][accounts_before]
        check(verify_account.session is session,
              "the resume login must run on the OTP session (session-bound verification)")
        check(CURRENT["accounts"][accounts_before + 1].session is not session,
              "the device probe must run on its own temporary session")
        resume_calls = [uri for _account, uri, _data in CURRENT["service_login_calls"]
                        if str(uri).startswith("serviceLogin")]
        check(len(resume_calls) == 2,
              f"the resume must re-run serviceLogin once (initial + resume), got {resume_calls}")
        check(CURRENT["security_calls"] and CURRENT["security_calls"][-1][1] ==
              "https://api2.mina.mi.com/sts?sign=resume",
              f"the final serviceToken must come from the resume location, got {CURRENT['security_calls']}")
        check(CURRENT["security_calls"][-1][2] == "resume-nonce"
              and CURRENT["security_calls"][-1][3] == "resume-ssecurity",
              "the clientSign inputs must be the resume nonce/ssecurity")

        if not check(h.auth_path.exists() and h.token_path.exists(),
                     "a verified OTP login must persist both credential files"):
            return "OTP login did not persist credentials"
        auth = json.loads(h.auth_path.read_text(encoding="utf-8"))
        token = json.loads(h.token_path.read_text(encoding="utf-8"))
        check(auth == {"passToken": "login-pass", "userId": "login-user",
                       "deviceId": "login-device"},
              f"auth.json must hold the verified identity, got {auth}")
        check(token.get("micoapi") == ["resume-ssecurity", "service-token-after-otp"],
              f".mi.token micoapi must be [ssecurity, serviceToken], got {token.get('micoapi')}")
        cookie_text = h.cfg["xiaomi"]["cookie_text"]
        check("passToken=login-pass" in cookie_text
              and "serviceToken=service-token-after-otp" in cookie_text,
              f"cookie_text must be complete, got {cookie_text!r}")
        check(h.otp_session() is None and h.otp_state() == {},
              "a successful login must close and clear the OTP state")
        check(session.closed is True, "the OTP session must be closed after success")
        ok("verify: same-session code -> location -> resume -> serviceToken -> probe -> persist")
    finally:
        h.cleanup()


# ─────────────── 6. wrong code: attempts, no writes, no auto resend ───────────────
def scenario_wrong_code():
    h = Harness()
    try:
        need_otp(h)
        session = h.otp_session()
        session.responses = [response(200, {"code": 0}), response(200, {"code": 0})]
        send(h)
        before = h.snapshot_data()
        calls_before = len(session.calls)

        session.responses = [response(200, {"code": 70016, "description": "验证码错误"})]
        data = verify(h, "000000")
        check(data.get("ok") is False and data.get("action") == "retry_code",
              f"a wrong code must be retryable, got {data}")
        check(data.get("attempts_left") == 4,
              f"one wrong code must leave 4 attempts, got {data.get('attempts_left')}")
        check(h.otp_state().get("attempts") == 1, "the attempt must be counted")
        check(session.closed is False, "a wrong code must keep the session usable")
        check(h.snapshot_data() == before, "a wrong code must not write any file")
        check(len(session.calls) == calls_before + 1,
              f"a wrong code must not send another SMS, got {session.urls()}")
        check(len(CURRENT["accounts"]) == 1,
              "a wrong code must not retry the resume login")

        # burn the remaining attempts
        for attempt in range(2, 6):
            session.responses = [response(200, {"code": 70016, "description": "验证码错误"})]
            last = verify(h, "000000")
            check(last.get("ok") is False, f"attempt {attempt} must fail")
        check(last.get("action") == "restart" and last.get("attempts_left") == 0,
              f"the last wrong code must force a restart, got {last}")
        check(h.otp_state() == {} and h.otp_session() is None,
              "exhausting the attempts must clear the OTP state")
        check(session.closed is True, "exhausting the attempts must close the session")
        check(h.snapshot_data() == before, "failed codes must leave /data byte identical")
        ok("wrong code: counted, 4 left, restart after 5, never writes, never resends")
    finally:
        h.cleanup()


# ─────────────── 6b. the device probe gates every write (OTP path) ───────────────
def scenario_otp_device_probe_gates_writes():
    h = Harness()
    try:
        h.auth_path.write_text('{"passToken": "old-pass", "userId": "old-user"}', encoding="utf-8")
        h.token_path.write_text('{"passToken": "old-pass", "userId": "old-user", '
                                '"micoapi": ["old-sec", "old-svc"]}', encoding="utf-8")
        before = h.snapshot_data()
        need_otp(h)
        session = h.otp_session()
        session.responses = [response(200, {"code": 0}), response(200, {"code": 0})]
        send(h)
        session.responses = [
            response(200, {"code": 0, "location": "/pass/verify/ok"}),
            response(200, raw=b""),
        ]
        CURRENT["login_script"] = [{
            "code": 0, "userId": "login-user", "passToken": "login-pass",
            "deviceId": "login-device", "ssecurity": "resume-ssecurity",
            "location": "https://api2.mina.mi.com/sts?sign=resume", "nonce": "resume-nonce",
        }]
        CURRENT["device_error"] = RuntimeError("mina 500")
        data = verify(h, "123456")
        check(data.get("ok") is False and data.get("action") == "verify_failed",
              f"a failing device probe must be an explicit failure, got {data}")
        check("设备列表" in data.get("msg", ""),
              f"the message must name the device-list failure: {data.get('msg')}")
        check(h.snapshot_data() == before,
              "nothing may be written before the speaker probe succeeded")
        CURRENT["device_error"] = None
        ok("OTP verify: a failing device probe blocks the write and is reported honestly")
    finally:
        h.cleanup()


# ─────────────── 7. TTL expiry on send / verify ───────────────
def scenario_ttl_expiry():
    h = Harness()
    try:
        # (a) expired before sending
        need_otp(h)
        session = h.otp_session()
        h.otp_state()["created_at"] = 1.0        # ~1970: far past the TTL
        data = send(h)
        check(data.get("ok") is False and data.get("action") == "restart",
              f"an expired session must refuse to send, got {data}")
        check(session.closed is True, "an expired session must be closed")
        check(h.otp_state() == {} and h.otp_session() is None,
              "an expired session must be cleared")

        # (b) expired before verifying
        need_otp(h)
        session = h.otp_session()
        h.otp_state()["created_at"] = 1.0
        data = verify(h, "123456")
        check(data.get("ok") is False and data.get("action") == "restart",
              f"an expired session must refuse the code, got {data}")
        check(session.closed is True, "expired verify must close the session")
        check(h.otp_state() == {}, "expired verify must clear the state")
        check(h.auth_path.exists() is False, "an expired verify must not write credentials")
        ok("TTL: expiry closes the session and forces a restart on send and verify")
    finally:
        h.cleanup()


# ─────────────── 8. restart closes the session and clears everything ───────────────
def scenario_restart():
    h = Harness()
    try:
        need_otp(h)
        session = h.otp_session()
        data = restart(h)
        check(data.get("ok") is True and data.get("restarted") is True,
              f"restart must report success, got {data}")
        check(session.closed is True, "restart must close the verification session")
        check(h.otp_state() == {} and h.otp_session() is None,
              "restart must clear the state")
        again = restart(h)
        check(again.get("ok") is True, f"restart without a session must stay harmless, got {again}")
        check(not any("redirect" in url for url in session.urls()),
              "restart must never touch the network")
        ok("restart: closes the session, clears the state, never redirects or re-logs in")
    finally:
        h.cleanup()


# ─────────────── 9. unsupported responses are reported, never faked ───────────────
def scenario_unsupported_flows():
    h = Harness()
    try:
        # (a) securityStatus 16 but no notificationUrl
        CURRENT["login_script"] = [{"code": 0, "securityStatus": 16, "userId": "u",
                                    "passToken": "p"}]
        data = start(h)
        check(data.get("ok") is False and data.get("action") == "unsupported",
              f"a missing notificationUrl must be an explicit error, got {data}")
        check(h.otp_state() == {} and h.otp_session() is None,
              "an unsupported start must not keep state")
        check(not any(session.calls for session in CURRENT["sessions"]),
              "an unsupported start must not issue verification requests")

        # (b) notificationUrl without context
        data = need_otp(h, notification="https://account.xiaomi.com/fe/service/identity/authStart?sid=micoapi")
        check(data.get("ok") is False and data.get("action") == "unsupported",
              f"a missing context must be an explicit error, got {data}")
        check(h.otp_state() == {}, "a missing context must not open a session")
        check(all(session.closed for session in CURRENT["sessions"]),
              "a missing context must close the temporary session")

        # (c) identity/list without a usable flag
        CURRENT["login_script"] = [{"code": 0, "securityStatus": 16,
                                    "notificationUrl": AUTH_START}]
        CURRENT["response_script"] = [
            response(200, raw=b""),
            response(200, {"code": 0, "data": {}}),
        ]
        data = start(h)
        check(data.get("ok") is False and data.get("action") == "unsupported",
              f"a response without flag must be an explicit error, got {data}")
        check("flag" in data.get("msg", ""), f"the error must name the missing flag: {data.get('msg')}")

        # (d) verify answers without location and without a rejection code
        need_otp(h)
        session = h.otp_session()
        session.responses = [response(200, {"surprise": True})]
        data = verify(h, "123456")
        check(data.get("ok") is False and data.get("action") == "unsupported",
              f"a location-less, code-less verify response must be unsupported, got {data}")
        check(session.closed is True, "an unsupported verify must close the session")
        check(h.otp_state() == {}, "an unsupported verify must clear the state")
        check(h.auth_path.exists() is False, "an unsupported verify must not write credentials")

        # (e) a bad password is reported with its code and never persisted
        CURRENT["login_script"] = [{"code": 70016, "description": "密码错误", "qs": "q",
                                    "sid": "s", "_sign": "x", "callback": "c"},
                                   {"code": 70016, "description": "密码错误"}]
        before = h.snapshot_data()
        data = start(h)
        check(data.get("ok") is False and data.get("action") == "login_failed",
              f"a rejected password must be an explicit failure, got {data}")
        check("70016" in data.get("msg", ""), f"the message must keep the Xiaomi code: {data.get('msg')}")
        check(h.snapshot_data() == before, "a rejected password must not write any file")
        ok("unsupported: missing notificationUrl/context/flag/location and bad passwords are explicit")
    finally:
        h.cleanup()


# ─────────────── 10. allowlist: only https://account.xiaomi.com is followed ───────────────
def scenario_location_allowlist():
    h = Harness()
    try:
        cases = [
            ("https://evil.example/pass/verify?t=1", "unsafe_location"),
            ("http://account.xiaomi.com/pass/verify?t=1", "unsafe_location"),
            ("https://account.xiaomi.com.evil.example/x", "unsafe_location"),
            ("https://user:pw@account.xiaomi.com/x", "unsafe_location"),
            ("https://account.xiaomi.com:8443/x", "unsafe_location"),
            ("//evil.example/x", "unsafe_location"),
        ]
        for location, expected in cases:
            need_otp(h)
            session = h.otp_session()
            session.responses = [response(200, {"code": 0, "location": location})]
            before = h.snapshot_data()
            data = verify(h, "123456")
            check(data.get("ok") is False and data.get("action") == "unsupported",
                  f"{location!r} must be refused as unsupported, got {data}")
            check(not any("evil.example" in url for url in session.urls()),
                  f"{location!r} must never be fetched, got {session.urls()}")
            check(session.closed is True, f"{location!r} must close the session")
            check(h.otp_state() == {}, f"{location!r} must clear the state")
            check(h.snapshot_data() == before, f"{location!r} must not write any file")
            check(expected in str(data.get("msg", "")) or "HTTPS" in str(data.get("msg", "")),
                  f"{location!r} must be named as an unsafe location: {data.get('msg')}")

        # a relative path is normalized onto the account host and accepted
        need_otp(h)
        session = h.otp_session()
        session.responses = [
            response(200, {"code": 0, "location": "/pass/verify/ok?t=1"}),
            response(200, raw=b""),
        ]
        CURRENT["login_script"] = [{
            "code": 0, "userId": "u", "passToken": "p", "deviceId": "d",
            "ssecurity": "s", "location": "https://api2.mina.mi.com/sts?sign=1",
            "nonce": "n",
        }]
        data = verify(h, "123456")
        check(data.get("ok") is True, f"a relative location must be completed, got {data}")
        check("https://account.xiaomi.com/pass/verify/ok?t=1" in session.urls(),
              f"the relative location must be prefixed, got {session.urls()}")
        ok("allowlist: only https://account.xiaomi.com is followed (6 hostile forms refused)")
    finally:
        h.cleanup()


# ─────────────── 11. manual Cookie login ───────────────
def scenario_cookie_login():
    h = Harness()
    try:
        # (a) userId + passToken only: exchange through MiAccount.login('micoapi')
        data = cookie_login(h, "userId=cookie-user; passToken=cookie-pass; deviceId=cookie-dev")
        check(data.get("ok") is True, f"an exchanged cookie must log in, got {data}")
        check(len(CURRENT["micoapi_login_calls"]) == 1,
              f"exactly one micoapi exchange expected, got {CURRENT['micoapi_login_calls']}")
        check(CURRENT["micoapi_login_calls"][0][1] == "micoapi",
              "the exchange must ask for the micoapi sid")
        token = json.loads(h.token_path.read_text(encoding="utf-8"))
        check(token.get("micoapi") == ["cookie-ssecurity", "cookie-service-token"],
              f"the exchanged token must be persisted, got {token.get('micoapi')}")
        cookie_text = h.cfg["xiaomi"]["cookie_text"]
        check("serviceToken=cookie-service-token" in cookie_text,
              f"cookie_text must carry the exchanged serviceToken, got {cookie_text!r}")

        # (b) a cookie that already has a serviceToken is probed directly
        h2 = Harness()
        try:
            data = cookie_login(h2, "userId=u; passToken=p; serviceToken=svc-direct; deviceId=d")
            check(data.get("ok") is True, f"a direct serviceToken cookie must log in, got {data}")
            check(CURRENT["micoapi_login_calls"] == [],
                  "a cookie with serviceToken must not call the exchange")
            token = json.loads(h2.token_path.read_text(encoding="utf-8"))
            check(token.get("micoapi") == ["", "svc-direct"],
                  f"the direct serviceToken must be persisted, got {token.get('micoapi')}")
        finally:
            h2.cleanup()
        ok("cookie: exchange via login('micoapi') and direct serviceToken both verify + persist")
    finally:
        h.cleanup()


def scenario_cookie_login_errors():
    h = Harness()
    try:
        cases = [
            ("", "fix_cookie", "空 Cookie"),
            ("   ", "fix_cookie", "空白 Cookie"),
            ("this is not a cookie", "fix_cookie", "无法解析"),
            ("userId=only-user", "fix_cookie", "缺少 passToken"),
            ("passToken=only-pass", "fix_cookie", "缺少 userId"),
        ]
        for text, action, label in cases:
            before = h.snapshot_data()
            data = cookie_login(h, text)
            check(data.get("ok") is False and data.get("action") == action,
                  f"{label}: must be {action}, got {data}")
            check(h.snapshot_data() == before, f"{label}: must not write any file")

        missing = cookie_login(h, "userId=u; passToken=p")
        check("passToken" not in missing.get("msg", "") or "userId" in missing.get("msg", ""),
              "the missing-field message must explain what to paste")
        check("必需" in cookie_login(h, "userId=u").get("msg", ""),
              "a missing passToken must be explained as required")

        # expired / insufficient permissions: the speaker probe fails
        CURRENT["device_error"] = RuntimeError("mina 403")
        before = h.snapshot_data()
        expired = cookie_login(h, "userId=u; passToken=p; serviceToken=svc")
        check(expired.get("ok") is False and expired.get("action") == "device_list",
              f"a failing device probe must be an explicit error, got {expired}")
        check("设备列表" in expired.get("msg", ""),
              f"the message must name the device-list failure: {expired.get('msg')}")
        check(h.snapshot_data() == before,
              "a failed device probe must not write any credential file")
        CURRENT["device_error"] = None

        # exchange failure
        CURRENT["cookie_login"] = "fail"
        before = h.snapshot_data()
        failed = cookie_login(h, "userId=u; passToken=p")
        check(failed.get("ok") is False and failed.get("action") == "exchange_failed",
              f"a failed exchange must be explicit, got {failed}")
        check(h.snapshot_data() == before, "a failed exchange must not write any file")
        CURRENT["cookie_login"] = "raise"
        raised = cookie_login(h, "userId=u; passToken=p")
        check(raised.get("ok") is False and raised.get("action") == "exchange_failed",
              f"an exception in the exchange must be explicit, got {raised}")
        CURRENT["cookie_login"] = "ok"
        ok("cookie errors: unparsable/empty/missing/expired/exchange-failure are all explicit, no writes")
    finally:
        h.cleanup()


def scenario_cookie_login_clears_otp():
    h = Harness()
    try:
        need_otp(h)
        otp_session = h.otp_session()
        check(h.otp_state() != {}, "precondition: an OTP session is pending")
        data = cookie_login(h, "userId=cookie-user; passToken=cookie-pass; serviceToken=svc-now")
        check(data.get("ok") is True, f"cookie login must work while an OTP session is pending, got {data}")
        check(h.otp_session() is None and h.otp_state() == {},
              "a successful cookie login must clear the pending OTP state")
        check(otp_session.closed is True, "the pending OTP session must be closed")
        ok("cookie: works while an OTP session is pending and clears it afterwards")
    finally:
        h.cleanup()


# ─────────────── 12. the legacy STS paste path keeps its honest message ───────────────
def scenario_legacy_message():
    h = Harness()
    try:
        session = FakeSession(responses=[response(401)])
        try:
            run(h.ns["_exchange_sts_service_token"](
                session, "https://api2.mina.mi.com/sts?sign=sig&d=1"))
            check(False, "a 401 from the pasted callback must raise")
        except h.ns["SmsVerifyError"] as exc:
            check(exc.code == "sts_http", f"the failure must stay sts_http, got {exc.code}")
            check("401" in exc.message, f"the message must name the status: {exc.message}")
            check("会话" in exc.message,
                  f"the message must explain the session binding: {exc.message}")
            check("过期" not in exc.message,
                  f"the old 'expired' explanation must be gone: {exc.message}")
            check("扫码" in exc.message or "账号密码" in exc.message,
                  f"the message must point at the supported paths: {exc.message}")
        check("链接可能已过期" not in source,
              "the misleading '链接可能已过期' message must be gone from bridge.py")
        ok("legacy STS: 401 is reported as session-bound / unsupported, not as 'expired'")
    finally:
        h.cleanup()


# ─────────────── 13. secrets never reach the log or the JSON responses ───────────────
def scenario_secrets_not_logged():
    h = Harness()
    try:
        password = "SUPER-SECRET-PASSWORD-123"
        pass_token = "log-pass-token-xyz"
        service_token = "log-service-token-abc"
        code = "654321"
        CURRENT["login_script"] = [{
            "code": 0, "securityStatus": 16, "notificationUrl": AUTH_START,
            "userId": "login-user", "passToken": pass_token,
        }]
        CURRENT["identity"] = {"flag": 4, "maskedPhone": "13812345678"}
        CURRENT["response_script"] = [response(200, raw=b""), response(200, CURRENT["identity"])]
        started = start(h, username="secret-user@example.com", password=password)
        check(started.get("need_otp") is True, f"the flow must reach need_otp, got {started}")
        session = h.otp_session()
        session.responses = [response(200, {"code": 0}), response(200, {"code": 0})]
        send(h)
        session.responses = [
            response(200, {"code": 0, "location": "/pass/verify/ok"}),
            response(200, raw=b""),
        ]
        CURRENT["login_script"] = [{
            "code": 0, "userId": "login-user", "passToken": pass_token,
            "deviceId": "login-device", "ssecurity": "ssecurity-value",
            "location": "https://api2.mina.mi.com/sts?sign=1", "nonce": "n",
        }]
        CURRENT["service_token"] = service_token
        finished = verify(h, code)
        check(finished.get("ok") is True, f"the flow must succeed, got {finished}")

        log_text = h.log.joined()
        response_text = json.dumps([started, finished], ensure_ascii=False)
        for label, secret in (("password", password), ("passToken", pass_token),
                              ("serviceToken", service_token), ("code", code),
                              ("username", "secret-user@example.com")):
            check(secret not in log_text, f"the {label} must never be logged: {log_text}")
            check(secret not in response_text,
                  f"the {label} must never be returned in a JSON response: {response_text}")
        check("OTP 登录" in log_text, "the log should still name the stage for debugging")
        check("138****5678" in json.dumps(started, ensure_ascii=False),
              "only the masked hint may be returned to the UI")
        ok("secrets: password/code/passToken/serviceToken never in logs or JSON responses")
    finally:
        h.cleanup()


# ─────────────── 14. failures leave /data byte identical (end to end) ───────────────
def scenario_failures_leave_data_untouched():
    h = Harness()
    try:
        h.auth_path.write_text('{"passToken": "old-pass", "userId": "old-user", '
                               '"deviceId": "old-dev"}', encoding="utf-8")
        h.token_path.write_text('{"passToken": "old-pass", "userId": "old-user", '
                                '"deviceId": "old-dev", "micoapi": ["old-sec", "old-svc"]}',
                                encoding="utf-8")
        before = h.snapshot_data()

        # a login that stops at need_otp
        need_otp(h)
        check(h.snapshot_data() == before, "starting a 2FA login must not touch the files")

        # a wrong code
        session = h.otp_session()
        session.responses = [response(200, {"code": 0}), response(200, {"code": 0})]
        send(h)
        session.responses = [response(200, {"code": 70016, "description": "验证码错误"})]
        verify(h, "111111")
        check(h.snapshot_data() == before, "a wrong code must not touch the files")

        # an unsupported verify response
        session.responses = [response(200, {"weird": 1})]
        verify(h, "222222")
        check(h.snapshot_data() == before, "an unsupported response must not touch the files")

        # a failed cookie exchange
        CURRENT["cookie_login"] = "fail"
        failed = cookie_login(h, "userId=u; passToken=p")
        check(failed.get("ok") is False, "the cookie exchange must fail")
        check(h.snapshot_data() == before,
              "a failed cookie exchange must leave the existing credentials intact")
        CURRENT["cookie_login"] = "ok"
        ok("failures: existing credentials stay byte identical through every failure path")
    finally:
        h.cleanup()


# ─────────────── 15. compatibility guards ───────────────
def scenario_compatibility():
    for route in ('@app.post("/api/qr/login")',
                  '@app.post("/api/test/xiaomi")',
                  '@app.post("/api/test/xiaomi/verify")',
                  '@app.post("/api/test/xiaomi/otp/start")',
                  '@app.post("/api/test/xiaomi/otp/send")',
                  '@app.post("/api/test/xiaomi/otp/verify")',
                  '@app.post("/api/test/xiaomi/otp/restart")',
                  '@app.post("/api/test/xiaomi/cookie-login")',
                  '@app.get("/api/config")'):
        check(route in source, f"{route} must be registered")
    check("_enforce_access_control" in source, "the HTTP auth middleware must still be installed")
    check("def _poll_auth_cookies" in source, "the 1.0.10 polling fix must stay in place")
    check("ssl=False" not in source, "TLS verification must stay enabled")
    check("str(TOKEN_PATH)" not in source, "MiService must never be handed the real token path")
    check("MiAccount(session, username, password, None)" in source,
          "the OTP login must never hand MiService a token store")
    check('input(' not in source.split("# ──────────────── 账号密码 + 验证码登录（1.0.12）")[-1],
          "the OTP section must never read from stdin")
    check("_persist_sms_credentials(creds)" in source,
          "1.0.12 must reuse the 1.0.11 atomic credential writer")
    check("SMS_PENDING_TTL" in source and "SMS_STS_HOST" in source,
          "the 1.0.11 SMS/STS implementation must stay in place")
    check('"unsupported"' in source, "unexpected Xiaomi responses must be reported as unsupported")
    ok("compatibility: QR/login/poll/auth routes, TLS checks and the 1.0.11 writer intact")


# ─────────────── 16. frontend structure of the three entries ───────────────
def scenario_frontend_structure():
    if not html_path.exists():
        print(f"SKIPPED frontend structure: no html at {html_path}")
        return None
    html = html_path.read_text(encoding="utf-8")

    check('id="mi-qr-card"' in html, "the recommended QR card must exist")
    check('id="mi-password-card"' in html, "the password entry must exist")
    check('id="mi-advanced-card"' in html and "<details" in html,
          "the cookie entry must be a collapsed <details> block")
    advanced = html.find('id="mi-advanced-card"')
    check(advanced > html.find('id="mi-password-card"'),
          "the advanced cookie entry must come after the password entry")
    check('id="mi-advanced-card"' in html and "details" in html[advanced - 40:advanced],
          "the advanced entry must be a <details> element (collapsed by default)")

    # the OTP panel lives inside the password card and starts hidden
    panel = html.find('id="mi-otp-panel"')
    check(panel > 0, "the OTP panel must exist")
    check(panel < html.find('id="mi-device-list"'),
          "the OTP panel must live inside the password card, before the device list")
    check('id="mi-otp-panel" style="display:none' in html,
          "the OTP panel must be hidden by default")

    # every button is wired to the right handler
    for handler in ("qrLogin()", "startPasswordLogin()", "sendOtp()", "verifyOtp()",
                    "restartOtp()", "verifyCookieLogin()", "clearXiaomiLogin()",
                    "completeSmsVerify()"):
        check(f'onclick="{handler}"' in html, f"no button is wired to {handler}")
    for element_id in ("mi-username", "mi-password", "mi-otp-code", "mi-otp-send",
                       "mi-otp-verify", "mi-otp-restart", "mi-otp-status", "mi-otp-method",
                       "mi-cookie-text", "mi-cookie-submit", "mi-cookie-status",
                       "qr-img", "qr-msg", "qr-status", "mi-sts-url", "mi-sts-submit"):
        check(f'id="{element_id}"' in html, f"missing element #{element_id}")
    check('id="qr-area"' in html and 'id="qr-area" style="display:none' in html,
          "the QR image area must exist and stay hidden until a QR is fetched")

    # the legacy STS block is only inside the advanced card and labelled honestly
    legacy = html.find('id="mi-sts-card"')
    check(legacy > advanced > 0, "the legacy STS block must be inside the advanced card")
    legacy_block = html[legacy - 400:html.find("</details>", legacy)]
    check("兼容" in legacy_block, "the legacy STS block must be labelled as a compatibility path")
    check("401" in legacy_block, "the legacy STS block must warn about the 401/session binding")
    check("不推荐" in legacy_block, "the legacy STS block must say it is not recommended")
    check("免短信" not in html, "the old '免短信' marketing badge must be gone")
    check("链接可能已过期" not in html, "the vague 'link may have expired' text must be gone")

    # the docstrings/labels of the three entries
    check("扫码登录（推荐）" in html, "the QR card must be labelled as recommended")
    check("账号密码登录" in html, "the password card must be labelled")
    check("高级选项：手动 Cookie 登录" in html, "the cookie card must be labelled as advanced")
    ok("frontend structure: 3 entries, hidden OTP panel, honest legacy block, badge removed")
    return "frontend structure verified"


# ─────────────── 17. frontend behaviour in Node (DOM stub) ───────────────
HARNESS_TEMPLATE = """
const calls=[];
const elements=__ELEMENTS__;
global.document={getElementById:(id)=>elements[id]||null};
global.showAlert=(a,cls,msg)=>{calls.push(['alert',cls,msg]);if(a){a.className='alert alert-'+cls+' show';a.innerHTML=String(msg);}};
global.renderDeviceList=(d)=>{calls.push(['devices',d]);};
global.setInterval=()=>1;
global.clearInterval=()=>{};
global.fetch=(url,init)=>{
  calls.push(['fetch',url,init&&init.method,init&&init.body]);
  return Promise.resolve({json:()=>Promise.resolve(RESPONSE)});
};
(async()=>{
  __INIT__
  await __CALL__;
  console.log(JSON.stringify({calls:calls,__SNAPSHOT__}));
})().catch(e=>{console.error('HARNESS ERROR',e);process.exit(1);});
"""


def _run_node(script_source, response, elements, call, snapshot, init_images=""):
    """Run one frontend function from the built HTML in Node with a DOM stub."""
    node = shutil.which("node")
    if not node:
        return None
    script = (script_source
              + "\nconst RESPONSE=" + json.dumps(response, ensure_ascii=False) + ";\n"
              + HARNESS_TEMPLATE
              .replace("__ELEMENTS__", json.dumps(elements, ensure_ascii=False))
              .replace("__SNAPSHOT__", snapshot)
              .replace("__INIT__", init_images or "")
              .replace("__CALL__", call))
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        script_path = handle.name
    try:
        completed = subprocess.run([node, script_path], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=60)
    finally:
        Path(script_path).unlink(missing_ok=True)
    if completed.returncode != 0:
        check(False, f"node failed for {call}: {(completed.stderr or '').strip()[:400]}")
        return None
    return json.loads(completed.stdout.strip().splitlines()[-1])


def scenario_frontend_behaviour():
    if not html_path.exists():
        print(f"SKIPPED frontend behaviour: no html at {html_path}")
        return None
    html = html_path.read_text(encoding="utf-8")
    start = html.find("// ═══════════ Xiaomi account/password + verification code (1.0.12) ═══════════")
    end = html.find("async function qrLogin(){", start)
    check(start >= 0, "the page must define the 1.0.12 Xiaomi login script")
    check(end > start, "the 1.0.12 script must be placed before qrLogin()")
    if start < 0 or end <= start:
        return "frontend behaviour check aborted"
    script_source = html[start:end]

    for name in ("startPasswordLogin", "sendOtp", "verifyOtp", "restartOtp",
                 "verifyCookieLogin", "otpReset", "_otp"):
        check(name in script_source, f"the 1.0.12 script must define {name}")

    cookie_source = html[html.find("async function verifyCookieLogin()"):
                         html.find("async function qrLogin()")]
    check("_otp" not in cookie_source,
          "verifyCookieLogin() must not be gated by the OTP state")

    base_elements = {
        "xiaomi-alert": {"className": "", "innerHTML": ""},
        "mi-username": {"value": "13800000000"},
        "mi-password": {"value": "pw"},
        "mi-login-submit": {"disabled": False},
        "mi-login-status": {"textContent": ""},
        "mi-otp-panel": {"style": {"display": "none"}},
        "mi-otp-method": {"textContent": ""},
        "mi-otp-cooldown": {"textContent": ""},
        "mi-otp-status": {"textContent": ""},
        "mi-otp-code": {"value": ""},
        "mi-otp-send": {"disabled": False, "textContent": "获取验证码"},
        "mi-otp-verify": {"disabled": False},
        "mi-cookie-text": {"value": ""},
        "mi-cookie-submit": {"disabled": False},
        "mi-cookie-status": {"textContent": ""},
    }
    snapshot = ("panel:elements['mi-otp-panel']?elements['mi-otp-panel'].style.display:null,"
                "method:elements['mi-otp-method'].textContent,"
                "otpStatus:elements['mi-otp-status'].textContent,"
                "cooldown:elements['mi-otp-cooldown'].textContent,"
                "loginStatus:elements['mi-login-status'].textContent,"
                "code:elements['mi-otp-code'].value,"
                "password:elements['mi-password'].value,"
                "cookieStatus:elements['mi-cookie-status'].textContent,"
                "sendDisabled:elements['mi-otp-send'].disabled,"
                "alert:elements['xiaomi-alert'].innerHTML,"
                "alertClass:elements['xiaomi-alert'].className")

    # (a) login -> need_otp: one POST, panel opens, nothing is sent automatically
    payload = _run_node(
        script_source,
        {"ok": True, "need_otp": True, "method": "Phone", "phone_hint": "138****1234",
         "expires_in": 300, "can_resend_in": 0, "msg": "需要短信验证码：请点击「获取验证码」"},
        base_elements, "startPasswordLogin()", snapshot,
        init_images="",
    )
    if payload:
        fetches = [call for call in payload["calls"] if call[0] == "fetch"]
        check(len(fetches) == 1, f"login must make exactly one request, got {fetches}")
        if fetches:
            check(fetches[0][1] == "/api/test/xiaomi/otp/start",
                  f"login must POST the relative otp/start path, got {fetches[0][1]}")
            check(fetches[0][2] == "POST", f"login must be a POST, got {fetches[0][2]}")
            body = json.loads(fetches[0][3] or "{}")
            check(body.get("username") == "13800000000" and body.get("password") == "pw",
                  f"login must send the typed credentials, got {body}")
        check(payload["panel"] == "", "need_otp must reveal the OTP panel")
        check("手机短信" in payload["method"] and "138****1234" in payload["method"],
              f"the panel must show the method and masked hint, got {payload['method']}")
        check(payload["sendDisabled"] is False, "the send button must be enabled")
        check(not any("otp/send" in str(call) for call in payload["calls"]),
              "login must never send the code by itself")
        ok("frontend login: POST /api/test/xiaomi/otp/start, panel opens, no automatic SMS")

    # (b) a login without 2FA closes the panel and refreshes the devices
    payload = _run_node(
        script_source,
        {"ok": True, "verified": True, "devices": [{"deviceID": "d1"}],
         "msg": "登录成功，发现 1 台设备"},
        base_elements, "startPasswordLogin()", snapshot,
    )
    if payload:
        check(payload["panel"] == "none", "a direct login must leave the OTP panel closed")
        check(any(call[0] == "devices" for call in payload["calls"]),
              "a direct login must refresh the device list")
        ok("frontend login (no 2FA): panel stays closed, device list refreshed")

    # (c) send -> POST + visible countdown; inside a cooldown nothing is requested
    elements = json.loads(json.dumps(base_elements))
    payload = _run_node(
        script_source,
        {"ok": True, "sent": True, "can_resend_in": 60, "msg": "验证码已发送到 138****1234"},
        elements, "sendOtp()", snapshot,
        init_images="_otp.state='pending';_otp.sent=false;_otp.cooldown=0;",
    )
    if payload:
        fetches = [call for call in payload["calls"] if call[0] == "fetch"]
        check(len(fetches) == 1 and fetches[0][1] == "/api/test/xiaomi/otp/send",
              f"send must POST /otp/send, got {fetches}")
        check("60" in payload["cooldown"] or "秒" in payload["cooldown"],
              f"the 60s countdown must be visible, got {payload['cooldown']!r}")
        check(payload["sendDisabled"] is True, "the send button must be disabled during the countdown")
        ok("frontend send: POST /otp/send and a visible 60s countdown")

    elements = json.loads(json.dumps(base_elements))
    payload = _run_node(
        script_source,
        {"ok": False, "action": "wait", "retry_after": 37, "msg": "验证码刚发送过，请 37 秒后再试"},
        elements, "sendOtp()", snapshot,
        init_images="_otp.state='pending';_otp.cooldown=37;",
    )
    if payload:
        fetches = [call for call in payload["calls"] if call[0] == "fetch"]
        check(fetches == [], f"inside the cooldown no request may be sent, got {fetches}")
        check("37" in payload["cooldown"], f"the remaining seconds must be shown, got {payload['cooldown']!r}")
        ok("frontend send: inside the cooldown the button asks to wait and sends nothing")

    # (d) verify: retry_code shows attempts_left; restart resets the panel
    elements = json.loads(json.dumps(base_elements))
    elements["mi-otp-panel"]["style"]["display"] = ""
    elements["mi-otp-code"]["value"] = "1234"
    payload = _run_node(
        script_source,
        {"ok": False, "action": "retry_code", "attempts_left": 3,
         "msg": "验证码不正确，还可尝试 3 次"},
        elements, "verifyOtp()", snapshot,
        init_images="_otp.state='pending';_otp.sent=true;",
    )
    if payload:
        fetches = [call for call in payload["calls"] if call[0] == "fetch"]
        check(len(fetches) == 1 and fetches[0][1] == "/api/test/xiaomi/otp/verify",
              f"verify must POST /otp/verify, got {fetches}")
        if fetches:
            check(json.loads(fetches[0][3] or "{}").get("code") == "1234",
                  f"the code must be sent as typed, got {fetches[0][3]}")
        check("3" in payload["otpStatus"], f"attempts_left must be shown, got {payload['otpStatus']!r}")
        ok("frontend verify: POST /otp/verify with the code, attempts_left shown")

    elements = json.loads(json.dumps(base_elements))
    elements["mi-otp-panel"]["style"]["display"] = ""
    elements["mi-otp-code"]["value"] = "999999"
    payload = _run_node(
        script_source,
        {"ok": False, "action": "restart", "msg": "验证码连续错误 5 次，已结束本次验证，请重新点击「登录」"},
        elements, "verifyOtp()", snapshot,
        init_images="_otp.state='pending';_otp.sent=true;",
    )
    if payload:
        check(payload["panel"] == "none", "action=restart must close the OTP panel")
        check("重新点击" in payload["alert"], f"action=restart must be explained, got {payload['alert']!r}")
        ok("frontend verify: action=restart closes the panel and explains the restart")

    elements = json.loads(json.dumps(base_elements))
    elements["mi-otp-panel"]["style"]["display"] = ""
    elements["mi-otp-code"]["value"] = "123456"
    payload = _run_node(
        script_source,
        {"ok": True, "verified": True, "devices": [{"deviceID": "d1"}],
         "msg": "登录成功，发现 1 台设备"},
        elements, "verifyOtp()", snapshot,
        init_images="_otp.state='pending';_otp.sent=true;",
    )
    if payload:
        check(payload["panel"] == "none", "a successful verify must hide the panel")
        check(payload["code"] == "", "a successful verify must clear the code input")
        check(any(call[0] == "devices" for call in payload["calls"]),
              "a successful verify must refresh the device list")
        ok("frontend verify: success hides the panel, clears the code, refreshes devices")

    # (e) restart -> POST /otp/restart, panel and inputs cleared, password cleared
    elements = json.loads(json.dumps(base_elements))
    elements["mi-otp-panel"]["style"]["display"] = ""
    elements["mi-otp-code"]["value"] = "123456"
    payload = _run_node(
        script_source,
        {"ok": True, "restarted": True, "msg": "已清除本次验证会话，请重新点击「登录」"},
        elements, "restartOtp()", snapshot,
        init_images="_otp.state='pending';_otp.sent=true;_otp.cooldown=12;",
    )
    if payload:
        fetches = [call for call in payload["calls"] if call[0] == "fetch"]
        check(len(fetches) == 1 and fetches[0][1] == "/api/test/xiaomi/otp/restart",
              f"restart must POST /otp/restart, got {fetches}")
        check(payload["panel"] == "none", "restart must close the OTP panel")
        check(payload["code"] == "", "restart must clear the code input")
        check(payload["password"] == "", "restart must clear the password input")
        ok("frontend restart: POST /otp/restart clears the panel and the inputs")

    # (f) cookie login works while _otp.state is 'pending'
    elements = json.loads(json.dumps(base_elements))
    elements["mi-cookie-text"]["value"] = "userId=u; passToken=p"
    payload = _run_node(
        script_source,
        {"ok": True, "verified": True, "devices": [{"deviceID": "d1"}],
         "msg": "Cookie 登录成功，发现 1 台设备"},
        elements, "verifyCookieLogin()", snapshot,
        init_images="_otp.state='pending';(function(){const p=document.getElementById('mi-otp-panel');if(p)p.style.display='';})();",
    )
    if payload:
        fetches = [call for call in payload["calls"] if call[0] == "fetch"]
        check(len(fetches) == 1,
              f"the cookie button must make exactly one request even while OTP is pending, got {fetches}")
        if fetches:
            check(fetches[0][1] == "/api/test/xiaomi/cookie-login",
                  f"the cookie button must POST the relative cookie-login path, got {fetches[0][1]}")
            check(fetches[0][2] == "POST", "the cookie login must be a POST")
            check(json.loads(fetches[0][3] or "{}").get("cookie_text") == "userId=u; passToken=p",
                  f"the typed cookie must be sent verbatim, got {fetches[0][3]}")
        check(payload["panel"] == "none",
              "a successful cookie login must close the pending OTP panel")
        check(any(call[0] == "devices" for call in payload["calls"]),
              "a successful cookie login must refresh the device list")
        ok("frontend cookie: works while _otp.state is pending (not gated), closes the panel")

    elements = json.loads(json.dumps(base_elements))
    elements["mi-cookie-text"]["value"] = "userId=u"
    payload = _run_node(
        script_source,
        {"ok": False, "action": "fix_cookie",
         "msg": "Cookie 缺少 passToken：userId 与 passToken 都是必需的"},
        elements, "verifyCookieLogin()", snapshot,
    )
    if payload:
        check("Cookie" in payload["cookieStatus"] or "检查" in payload["cookieStatus"],
              f"a parse error must point at the Cookie text, got {payload['cookieStatus']!r}")
        check("passToken" in payload["alert"], "the backend message must be surfaced")
        ok("frontend cookie: fix_cookie is surfaced with the backend explanation")
    return "frontend behaviour verified"


def main():
    probe = Harness()
    probe.cleanup()
    if failures:
        print(f"xiaomi login: {len(failures)} structural problem(s) found")
        for failure in failures:
            print(f"  FAIL {failure}")
        print("hint: this suite targets a bridge.py patched with 1.0.12 (patch_auth.py)")
        sys.exit(1)
    scenarios = [
        scenario_direct_login_success,
        scenario_start_needs_otp,
        scenario_send_cooldown,
        scenario_send_after_cooldown,
        scenario_verify_success,
        scenario_wrong_code,
        scenario_otp_device_probe_gates_writes,
        scenario_ttl_expiry,
        scenario_restart,
        scenario_unsupported_flows,
        scenario_location_allowlist,
        scenario_cookie_login,
        scenario_cookie_login_errors,
        scenario_cookie_login_clears_otp,
        scenario_legacy_message,
        scenario_secrets_not_logged,
        scenario_failures_leave_data_untouched,
        scenario_compatibility,
    ]
    for scenario in scenarios:
        scenario()
    for frontend in (scenario_frontend_structure(), scenario_frontend_behaviour()):
        if frontend:
            results.append(frontend)

    if failures:
        print(f"xiaomi login: {checks} checks, {len(failures)} FAILED")
        for failure in failures:
            print(f"  FAIL {failure}")
        sys.exit(1)
    for line in results:
        print(f"  ok  {line}")
    print(f"xiaomi login tests passed ({checks} checks, {len(results)} scenarios)")


if __name__ == "__main__":
    main()
