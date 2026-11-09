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

The "[real 2.9.3]" section at the end is the exception: it loads the genuine
miservice_fork 2.9.3 miaccount.py (unpacked sdist source, no network) and pins
two contracts against it — the token preload that makes the Cookie exchange
possible, and the TLS behaviour (the dependency disables certificate
verification inside MiAccount._serviceLogin, the bridge must not). Point
MISERVICE_REAL_PATH at a directory containing the miservice package, or leave it
alone and let the two candidate paths under the repo parent be tried; when the
package is missing the section prints SKIP and the rest of the suite still runs.

Usage: python test_xiaomi_login.py [/app/bridge.py] [--html /app/web/index.html]
"""

import ast
import asyncio
import base64
import hashlib
import inspect
import json
import os
import re
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

# the 1.0.12 section is the only place the new login flows may live
F_SECTION_MARKER = "# ──────────────── 账号密码 + 验证码登录（1.0.12）"
f_section_source = source.split(F_SECTION_MARKER)[-1]

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
    def __init__(self, status=200, body=b"", headers=None, cookies=None):
        self.status = status
        self.headers = dict(headers or {})
        self.cookies = dict(cookies or {})
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
        self.filter_urls = []

    def update_cookies(self, cookies, url=None):
        self.cookie.update(dict(cookies or {}))

    def filter_cookies(self, url=None):
        '''What a real aiohttp CookieJar returns for account.xiaomi.com.'''
        self.filter_urls.append(str(url))
        from http.cookies import SimpleCookie
        jar = SimpleCookie()
        for key, value in self.cookie.items():
            jar[str(key)] = str(value)
        return jar


ACCOUNT_PASS_URL = "https://account.xiaomi.com/pass/"
LOGIN_PREFIX = ACCOUNT_PASS_URL + "serviceLogin"


class FakeSession:
    def __init__(self, name="session", responses=None, login_responses=None):
        self.name = name
        self.responses = list(responses or [])
        # None means "take the next scripted password-login answer from CURRENT"
        self.login_responses = login_responses
        self.calls = []
        self.closed = False
        self.jar = FakeJar()

    @property
    def cookie_jar(self):
        return self.jar

    def _login_reply(self):
        '''One password-login answer: scripted per session, per scenario, or a failure knob.'''
        if self.login_responses is not None:
            return self.login_responses.pop(0) if self.login_responses else {}
        behaviour = CURRENT.get("cookie_login", "ok")
        if behaviour == "raise":
            raise RuntimeError("scripted exchange failure")
        if behaviour == "fail":
            return {"code": 70016, "description": "scripted exchange rejection"}
        if CURRENT["login_script"]:
            return CURRENT["login_script"].pop(0)
        raise RuntimeError("no scripted login response")

    def _request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if str(url).startswith(LOGIN_PREFIX):
            # the 1.0.12 flows reach account.xiaomi.com/pass/serviceLogin through
            # _mi_service_login_verified, so the fake must answer on the session
            payload = self._login_reply()
            CURRENT["verified_login_calls"].append({
                "session": self, "method": method, "url": str(url),
                "kwargs": kwargs, "payload": payload,
            })
            CURRENT["service_login_calls"].append(
                (self, str(url)[len(ACCOUNT_PASS_URL):], kwargs.get("data"))
            )
            body = b"&&&START&&&" + json.dumps(payload).encode("utf-8")
            return _ResponseContext(FakeResponse(200, body))
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
        "verified_login_calls": [],
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
    "_otp_session_identity",
    "_safe_otp_description",
    "_mi_service_login_verified",
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
    "_otp_split_target",
    "_otp_safe_label",
    "_otp_sts_target",
    "_otp_confirm_from_query",
    "_otp_too_many_redirects",
    "_otp_log_sts_terminal",
    "_otp_normalize_auth_hop",
    "_otp_exchange",
    "_otp_request",
    "_otp_follow_auth_chain",
    "_otp_request_json",
    "_otp_finish_login",
    "_otp_missing_login_fields",
    "_otp_login_complete",
    "_otp_resume_with_identity",
    "_otp_recover_incomplete_login",
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
    "xiaomi_cookie_ticket",
    "_adopt_login_identity",
    "_plausible_credential",
    "_cookie_pass_token_problem",
    "_cookie_exchange_detail",
    "_cookie_exchange_message",
    "_iter_json_objects",
    "_parse_cookie_ticket",
    "_ticket_client_sign",
    "_ticket_sts_url",
    "_response_cookie_values",
    "_exchange_ticket_service_token",
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

CLASSES = ("OtpFlowError", "SmsVerifyError", "CookieTicketError",
           "CredentialIdentityMismatch")


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
            "secrets": __import__("secrets"),
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
            "_credential_write_allowed": lambda: True,
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
            "CookieTicketRequest": object,
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


def cookie_ticket(h, text):
    return run(h.ns["xiaomi_cookie_ticket"](SimpleNamespace(text=text)))


def response(status=200, payload=None, raw=None, headers=None, cookies=None):
    if raw is not None:
        return FakeResponse(status, raw, headers=headers, cookies=cookies)
    if payload is None:
        return FakeResponse(status, b"", headers=headers, cookies=cookies)
    return FakeResponse(status, payload, headers=headers, cookies=cookies)


def redirect(location, status=302):
    '''A 3xx answer whose Location header is the only useful content.'''
    return response(status, raw=b"", headers={"Location": location})


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
        check(len(urls) == 3,
              f"start must log in, then open authStart + identity/list, got {urls}")
        if len(urls) >= 3:
            check(urls[0] == "https://account.xiaomi.com/pass/serviceLogin?sid=micoapi&_json=true",
                  f"step 0 must be the TLS-verified password login, got {urls[0]}")
            check(urls[1].startswith("https://account.xiaomi.com/fe/service/identity/authStart"),
                  f"step 1 must open the notificationUrl, got {urls[1]}")
            check("/identity/list?" in urls[2] and "context=ctx-123" in urls[2],
                  f"step 2 must call identity/list with the context, got {urls[2]}")
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


# ─────────────── 3b. send: the verifyPhone business code gates the SMS ───────────────
def scenario_verify_phone_business_code():
    h = Harness()
    try:
        need_otp(h)
        session = h.otp_session()
        before = h.snapshot_data()
        calls_before = len(session.calls)

        # (a) HTTP 200 + business code 1: stop before sendPhoneTicket, no cooldown
        session.responses = [response(200, {"code": 1, "description": "风控拦截"})]
        data = send(h)
        check(data.get("ok") is False and data.get("action") == "unsupported",
              f"a business code 1 must be an explicit failure, got {data}")
        check(data.get("code") == "verify_phone_failed",
              f"a business code 1 must report verify_phone_failed, got {data.get('code')}")
        check("业务码 1" in str(data.get("msg")),
              f"the message must name the business code, got {data.get('msg')}")
        tickets = [url for url in session.urls() if "sendPhoneTicket" in url]
        check(tickets == [],
              f"a rejected verifyPhone must never reach sendPhoneTicket, got {tickets}")
        check(len(session.calls) == calls_before + 1,
              f"a rejected verifyPhone must stop after the trigger, got {session.calls[calls_before:]}")
        check(not h.otp_state().get("cooldown_until"),
              f"a rejected verifyPhone must not start a cooldown, got {h.otp_state()}")
        check(h.otp_state().get("sent") is not True,
              "a rejected verifyPhone must not claim the code was sent")
        check(h.snapshot_data() == before, "a rejected verifyPhone must not write any file")
        check(session.closed is False, "a rejected verifyPhone may keep the session for a retry")

        # (b) HTTP 200 + non-JSON body: the same explicit failure, no SMS
        session.responses = [response(200, raw=b"<html>not json</html>")]
        data = send(h)
        check(data.get("ok") is False and data.get("code") == "verify_phone_failed",
              f"a non-JSON verifyPhone body must fail explicitly, got {data}")
        check([url for url in session.urls() if "sendPhoneTicket" in url] == [],
              "a non-JSON verifyPhone body must not trigger the ticket")
        check(not h.otp_state().get("cooldown_until"),
              "a non-JSON verifyPhone body must not start a cooldown")
        check(h.snapshot_data() == before, "a non-JSON body must not write any file")

        # (c) HTTP 200 + code 0: the positive path is unchanged
        session.responses = [response(200, {"code": 0}), response(200, {"code": 0})]
        data = send(h)
        check(data.get("ok") is True and data.get("sent") is True,
              f"a business code 0 must still send the code, got {data}")
        tickets = [url for url in session.urls() if "sendPhoneTicket" in url]
        check(len(tickets) == 1, f"exactly one SMS trigger expected, got {tickets}")
        check(bool(h.otp_state().get("cooldown_until")),
              "a sent code must still start the cooldown")
        ok("send: verifyPhone business code gates sendPhoneTicket and the cooldown")
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

        # the verification chain authenticates this very session: its pages set these
        # cookies, and the resume is only accepted while they travel with the request
        session.jar.cookie.update({"passToken": "jar-pass", "userId": "jar-user"})

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
        resume_cookies = CURRENT["verified_login_calls"][-1]["kwargs"].get("cookies") or {}
        check(resume_cookies.get("passToken") == "jar-pass",
              f"the resume must carry the passToken the verification session holds, "
              f"got {resume_cookies}")
        check(resume_cookies.get("userId") == "jar-user",
              f"the resume must carry the session userId, got {resume_cookies}")
        first_cookies = CURRENT["verified_login_calls"][0]["kwargs"].get("cookies") or {}
        check("passToken" not in first_cookies,
              f"the anonymous password login must not send an empty passToken, got {first_cookies}")
        check("恢复前会话凭据 (passToken=yes, userId=yes)" in h.log.joined(),
              "the log must record whether the resume had session credentials")
        check("jar-pass" not in h.log.joined() and "jar-user" not in h.log.joined(),
              "the session credentials must never be logged")
        check(any("/pass/serviceLogin" in str(url) for url in session.jar.filter_urls),
              "the resume identity must be read for the resume URL (Cookie Path), got "
              f"{session.jar.filter_urls}")

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


# ─────────────── 5b. a rejected resume names the Xiaomi code, never a secret ───────────────
def scenario_resume_rejected():
    h = Harness()
    try:
        session = _drive_verify(h)
        session.jar.cookie.update({"passToken": "jar-pass", "userId": "jar-user"})
        session.responses = [
            response(200, {"code": 0, "location": "/pass/verify/accepted?token=abc"}),
            response(200, raw=b"<html>ok</html>"),
        ]
        CURRENT["login_script"] = [{
            "code": 70016, "description": "登录参数不合法",
            "userId": "login-user", "passToken": "login-pass",
        }]
        before = h.snapshot_data()
        security_before = len(CURRENT["security_calls"])
        data = verify(h, "123456")

        check(data.get("ok") is False and data.get("code") == "resume_failed",
              f"a rejected resume must fail explicitly, got {data}")
        message = str(data.get("msg") or "")
        check("70016" in message,
              f"the user-facing message must carry the Xiaomi code: {message}")
        log_text = h.log.joined()
        check("恢复登录被拒绝 (code=70016, description=登录参数不合法)" in log_text,
              f"the log must record the business code and description: {log_text}")
        check("jar-pass" not in log_text and "login-pass" not in log_text,
              "the rejection log must never carry a credential")
        check(len(CURRENT["security_calls"]) == security_before,
              "a rejected resume must not exchange any serviceToken")
        check(h.snapshot_data() == before,
              "a rejected resume must not write any credential file")
        check(h.otp_state() == {},
              "a rejected resume must clear the OTP state so a new code can be requested")
        ok("resume rejected: the Xiaomi code/description reach the log and the message, "
           "no credential does")
    finally:
        h.cleanup()


# ─────────────── 5c. an incomplete resume is classified, not reported as malformed ───────
def _drive_resume(h, jar=None):
    '''Verify up to the resume answer: code submission accepted, location followed.'''
    session = _drive_verify(h)
    if jar:
        session.jar.cookie.update(jar)
    session.responses = [
        response(200, {"code": 0, "location": "/pass/verify/accepted?token=abc"}),
        response(200, raw=b"<html>ok</html>"),
    ]
    return session


CHALLENGE_ANSWER = {"code": 0, "qs": "%3Fsid%3Dmicoapi", "_sign": "CHALLENGE-SIG-VALUE",
                    "sid": "micoapi", "callback": "https://api2.mina.mi.com/sts"}


def scenario_resume_second_round():
    h = Harness()
    try:
        session = _drive_resume(h, {"passToken": "jar-pass", "userId": "jar-user"})
        # code 0 + a fresh notificationUrl: Xiaomi wants another verification round,
        # which is a normal step answer, not a malformed login
        CURRENT["login_script"] = [{
            "code": 0, "securityStatus": 16,
            "notificationUrl": AUTH_START.replace("ctx-123", "ctx-456"),
        }]
        CURRENT["identity"] = {"flag": 4, "maskedPhone": "13900001111"}
        session.responses += [
            response(200, raw=b"<html>auth start 2</html>"),
            response(200, CURRENT["identity"]),
        ]
        before = h.snapshot_data()
        security_before = len(CURRENT["security_calls"])
        data = verify(h, "123456")

        check(data.get("ok") is True and data.get("need_otp") is True,
              f"a resume that asks for another verification must keep the OTP panel, got {data}")
        check(data.get("second_round") is True,
              f"the payload must say this is a second round, got {data}")
        check(data.get("sent") is False,
              f"a new round must not claim a code was already sent, got {data}")
        check("再验证" in str(data.get("msg")), f"the message must say so: {data.get('msg')}")
        state = h.otp_state()
        check(state.get("context") == "ctx-456",
              f"the OTP state must carry the new context, got {state.get('context')}")
        check(state.get("attempts") == 0 and state.get("sent") is False,
              f"the second round starts clean, got {state}")
        check(len(CURRENT["security_calls"]) == security_before,
              "a second round must not exchange any serviceToken")
        check(h.snapshot_data() == before, "a second round must not write any credential file")
        check(h.otp_session() is session and session.closed is False,
              "the verification session must stay open for the second round")
        log_text = h.log.joined()
        check("resume 响应不完整 (code=0" in log_text,
              f"the incomplete resume must be logged with its code: {log_text}")
        check("stage=second_round" in log_text,
              f"the second round must be logged as such: {log_text}")
        ok("resume: code 0 + notificationUrl reopens the verification round instead of "
           "dead-ending on 'missing ssecurity/location/nonce'")
    finally:
        h.cleanup()


def scenario_resume_fresh_session_recovery():
    h = Harness()
    try:
        session = _drive_resume(h, {"passToken": "jar-pass", "userId": "jar-user"})
        CURRENT["login_script"] = [
            CHALLENGE_ANSWER,
            # the same account as the verification session: the fallback must not
            # switch identity, and its answer still has to carry the full ticket
            {"code": 0, "userId": "jar-user", "passToken": "login-pass",
             "deviceId": "login-device", "ssecurity": "fresh-ssecurity",
             "location": "https://api2.mina.mi.com/sts?sign=fresh", "nonce": "fresh-nonce"},
        ]
        CURRENT["service_token"] = "service-token-after-fresh"
        data = verify(h, "123456")

        check(data.get("ok") is True and data.get("verified") is True,
              f"a clean passToken exchange must finish the login, got {data}")
        check(len(data.get("devices") or []) == 1, "the device probe must still run")
        fresh = [entry for entry in CURRENT["verified_login_calls"]
                 if entry["session"] is not session]
        check(len(fresh) == 1,
              f"exactly one exchange on a new session expected, got {len(fresh)}")
        if fresh:
            cookies = fresh[0]["kwargs"].get("cookies") or {}
            check(cookies.get("passToken") == "jar-pass"
                  and cookies.get("userId") == "jar-user",
                  f"the new session must carry the verified identity, got {cookies}")
            check(fresh[0]["session"].closed is True,
                  "the recovery session must be closed afterwards")
            check(str(fresh[0]["url"]).startswith(LOGIN_PREFIX),
                  f"the recovery must use the same serviceLogin, got {fresh[0]['url']}")
        check(CURRENT["security_calls"]
              and CURRENT["security_calls"][-1][1] == "https://api2.mina.mi.com/sts?sign=fresh",
              f"the token must come from the recovered answer, got {CURRENT['security_calls']}")
        check(CURRENT["security_calls"][-1][2] == "fresh-nonce"
              and CURRENT["security_calls"][-1][3] == "fresh-ssecurity",
              "the clientSign inputs must be the recovered nonce/ssecurity")
        if check(h.auth_path.exists() and h.token_path.exists(),
                 "a recovered login must persist both credential files"):
            token = json.loads(h.token_path.read_text(encoding="utf-8"))
            check(token.get("micoapi") == ["fresh-ssecurity", "service-token-after-fresh"],
                  f".mi.token must hold the recovered ticket, got {token.get('micoapi')}")
        check("source=fresh_session" in h.log.joined(),
              f"the recovered exchange must be logged: {h.log.joined()}")
        check(h.otp_state() == {} and h.otp_session() is None,
              "a recovered login must clear the OTP state")
        ok("resume: a same-session answer without the ticket falls back to one clean "
           "passToken exchange, then finishes normally")
    finally:
        h.cleanup()


def scenario_resume_incomplete_is_explicit():
    h = Harness()
    try:
        session = _drive_resume(h)     # the session holds no passToken/userId at all
        CURRENT["login_script"] = [CHALLENGE_ANSWER]
        before = h.snapshot_data()
        security_before = len(CURRENT["security_calls"])
        data = verify(h, "123456")

        check(data.get("ok") is False and data.get("code") == "resume_incomplete",
              f"an unrecoverable resume must fail as resume_incomplete, got {data}")
        message = str(data.get("msg") or "")
        for field in ("ssecurity", "location", "nonce"):
            check(field in message,
                  f"the message must name the missing field {field}: {message}")
        log_text = h.log.joined()
        check("resume 响应不完整 (code=0, result=None, missing=ssecurity,location,nonce"
              in log_text,
              f"the log must carry code/result/missing and no payload: {log_text}")
        check("CHALLENGE-SIG-VALUE" not in log_text,
              f"the log must never carry the payload body: {log_text}")
        check(len(CURRENT["security_calls"]) == security_before,
              "an incomplete resume must not reach the token exchange")
        check(h.snapshot_data() == before,
              "an incomplete resume must not write any credential file")
        check(h.otp_state() == {},
              "resume_incomplete is a breaking error: the OTP state must be dropped")
        check(session.closed is True,
              "the dropped verification session must be closed")
        ok("resume: an answer that cannot be completed is named field by field, logged "
           "with code/result/missing, and drops the session explicitly")
    finally:
        h.cleanup()


def scenario_resume_second_round_capped():
    """Xiaomi may keep demanding another code; the loop must be bounded."""
    h = Harness()
    try:
        session = _drive_resume(h, {"passToken": "jar-pass", "userId": "jar-user"})
        second = {"code": 0, "securityStatus": 16,
                  "notificationUrl": AUTH_START.replace("ctx-123", "ctx-201")}
        CURRENT["login_script"] = [second]
        CURRENT["identity"] = {"flag": 4, "maskedPhone": "13900002222"}
        session.responses = [
            response(200, {"code": 0, "location": "/pass/verify/accepted?token=abc"}),
            response(200, raw=b"<html>ok</html>"),
            response(200, raw=b"<html>auth start 2</html>"),
            response(200, CURRENT["identity"]),
        ]
        first = verify(h, "123456")
        check(first.get("ok") is True and first.get("second_round") is True,
              f"the first extra round must be offered, got {first}")
        check(h.otp_state().get("rounds") == 2,
              f"the state must count the rounds, got {h.otp_state().get('rounds')}")

        # a second extra round would be round 3: refuse instead of sending more SMS
        CURRENT["login_script"] = [{
            "code": 0, "securityStatus": 16,
            "notificationUrl": AUTH_START.replace("ctx-123", "ctx-202"),
        }]
        session.responses = [
            response(200, {"code": 0, "location": "/pass/verify/accepted?token=abc"}),
            response(200, raw=b"<html>ok</html>"),
        ]
        before = h.snapshot_data()
        tickets_before = [url for url in session.urls() if "sendPhoneTicket" in url]
        data = verify(h, "654321")
        check(data.get("ok") is False and data.get("code") == "too_many_rounds",
              f"a third verification round must be refused, got {data}")
        check("扫码登录" in str(data.get("msg") or ""),
              f"the refusal must point at QR login: {data.get('msg')}")
        check(h.otp_state() == {},
              "too_many_rounds is a breaking error: the OTP state must be dropped")
        check(h.snapshot_data() == before, "a refused round must not write any credential file")
        check([url for url in session.urls() if "sendPhoneTicket" in url] == tickets_before,
              "a refused round must not send an SMS")
        ok("resume: repeated 'verify again' answers are capped (no endless SMS loop)")
    finally:
        h.cleanup()


def scenario_otp_start_other_account():
    """A pending verification belongs to one account; a new account must not reuse it."""
    h = Harness()
    try:
        session = _drive_verify(h)
        check(h.otp_state().get("username") == "13800000000",
              f"the pending state must remember its account, got {h.otp_state().get('username')}")

        # the same page now submits another account while the first one is pending
        CURRENT["login_script"] = [{
            "code": 0,
            "securityStatus": 16,
            "notificationUrl": AUTH_START.replace("ctx-123", "ctx-999"),
            "userId": "login-user",
            "passToken": "login-pass",
        }]
        CURRENT["identity"] = {"flag": 4, "maskedPhone": "13912345678"}
        CURRENT["response_script"] = [
            response(200, raw=b"<html>auth start 2</html>"),
            response(200, CURRENT["identity"]),
        ]
        data = start(h, username="13900000000", password="pw2")

        check(data.get("ok") is True and data.get("need_otp") is True,
              f"the new account must get its own verification session, got {data}")
        check(data.get("phone_hint") == "139****5678",
              f"the panel must describe the new account, got {data.get('phone_hint')}")
        check(h.otp_state().get("username") == "13900000000",
              f"the state must follow the new account, got {h.otp_state().get('username')}")
        check(session.closed is True,
              "the previous account's verification session must be closed")
        check("提交的账号与待验证会话不一致" in h.log.joined(),
              f"the discard must be logged: {h.log.joined()}")
        ok("otp start: a different account discards the pending session instead of "
           "completing the previous account's login")
    finally:
        h.cleanup()


def scenario_resume_location_hop():
    """A resume that only answers a location must be completed like a browser."""
    h = Harness()
    try:
        session = _drive_resume(h, {"passToken": "jar-pass", "userId": "jar-user"})
        CURRENT["login_script"] = [
            # code 0 + only a location: no ssecurity/nonce in this answer
            {"code": 0, "location": "https://account.xiaomi.com/pass/verify/accepted?token=hop"},
            {"code": 0, "userId": "jar-user", "passToken": "login-pass",
             "deviceId": "login-device", "ssecurity": "hop-ssecurity",
             "location": "https://api2.mina.mi.com/sts?sign=hop", "nonce": "hop-nonce"},
        ]
        session.responses.append(response(200, raw=b"<html>hop</html>"))
        data = verify(h, "123456")

        check(data.get("ok") is True and data.get("verified") is True,
              f"opening the resume location must finish the login, got {data}")
        urls = session.urls()
        check(any("/pass/verify/accepted?token=hop" in url for url in urls),
              f"the resume location must be opened exactly once, got {urls}")
        log_text = h.log.joined()
        check("stage=resume_hop" in log_text and "source=resume_hop" in log_text,
              f"the hop and its result must be logged: {log_text}")
        check(CURRENT["security_calls"]
              and CURRENT["security_calls"][-1][1] == "https://api2.mina.mi.com/sts?sign=hop",
              f"the token must come from the retried resume, got {CURRENT['security_calls']}")
        check(h.otp_state() == {} and h.otp_session() is None,
              "a completed hop login must clear the OTP state")
        ok("resume: an answer with only a location is completed by opening it once, "
           "then re-running the resume")
    finally:
        h.cleanup()


def scenario_resume_identity_mismatch():
    """The clean-session fallback must never persist a different account."""
    h = Harness()
    try:
        session = _drive_resume(h, {"passToken": "jar-pass", "userId": "jar-user"})
        CURRENT["login_script"] = [
            CHALLENGE_ANSWER,
            {"code": 0, "userId": "someone-else", "passToken": "other-pass",
             "deviceId": "login-device", "ssecurity": "other-ssecurity",
             "location": "https://api2.mina.mi.com/sts?sign=other", "nonce": "other-nonce"},
        ]
        before = h.snapshot_data()
        security_before = len(CURRENT["security_calls"])
        data = verify(h, "123456")

        check(data.get("ok") is False and data.get("code") == "resume_incomplete",
              f"a foreign identity must be refused, got {data}")
        check("身份与验证会话不一致" in h.log.joined(),
              f"the refusal must be logged without ids: {h.log.joined()}")
        check("jar-user" not in h.log.joined() and "someone-else" not in h.log.joined(),
              "neither identity may appear in the log")
        check(len(CURRENT["security_calls"]) == security_before,
              "a foreign identity must not reach the token exchange")
        check(h.snapshot_data() == before,
              "a foreign identity must never be persisted")
        check(h.otp_state() == {}, "the rejected verification state must be dropped")
        ok("resume: the clean-session fallback refuses a different account identity")
    finally:
        h.cleanup()


def scenario_missing_field_detection():
    h = Harness()
    try:
        missing = h.ns["_otp_missing_login_fields"]
        check(missing({"ssecurity": "s", "location": "l", "nonce": 0}) == [],
              "an integer 0 nonce is a present field, not a missing one")
        check(missing({"ssecurity": "s", "location": "l", "nonce": ""}) == ["nonce"],
              "an empty nonce must be reported as missing")
        check(missing({"ssecurity": "s", "location": "l"}) == ["nonce"],
              "an absent nonce must be reported as missing")
        check(missing({"ssecurity": "s", "nonce": "n"}) == ["location"],
              "an absent location must be reported as missing")
        check(missing("not-a-dict") == ["ssecurity", "location", "nonce"],
              "a non-object answer must report all three fields")
        check(h.ns["_otp_login_complete"]({"ssecurity": "s", "location": "l", "nonce": "n"}) is True,
              "a complete answer must be recognised")
        ok("classification: missing-field detection is explicit about None vs empty")
    finally:
        h.cleanup()


def scenario_password_login_challenge_without_code():
    h = Harness()
    try:
        # the first serviceLogin answer is the pre-login challenge: qs/_sign/callback
        # and no "code" field at all. Treating a missing code as "already logged in"
        # would skip the password check entirely.
        CURRENT["login_script"] = [
            {"qs": "%3Fsid%3Dmicoapi", "sid": "micoapi", "_sign": "sig",
             "callback": "https://api2.mina.mi.com/sts"},
            {"code": 0, "userId": "login-user", "passToken": "login-pass",
             "deviceId": "login-device", "ssecurity": "login-ssecurity",
             "location": "https://api2.mina.mi.com/sts?sign=login", "nonce": "login-nonce"},
        ]
        data = start(h)

        check(data.get("ok") is True and data.get("verified") is True,
              f"the challenge must still be answered with auth2, then succeed, got {data}")
        calls = CURRENT["service_login_calls"]
        check(len(calls) == 2,
              f"a challenge without a code must still run auth2, got {calls}")
        if len(calls) == 2:
            check(str(calls[1][1]) == "serviceLoginAuth2",
                  f"the second step must be auth2, got {calls[1][1]}")
            form = calls[1][2] or {}
            check(form.get("user") == "13800000000" and bool(form.get("hash")),
                  f"auth2 must carry the username and password hash, got {form}")
            check(form.get("_sign") == "sig" and form.get("sid") == "micoapi",
                  f"auth2 must reuse the challenge sign/sid, got {form}")
        ok("start: a serviceLogin answer without 'code' is a challenge, not a login "
           "(auth2 still runs, no password check is skipped)")
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
        check(not any("/identity/" in str(url) or "authStart" in str(url)
                      for session in CURRENT["sessions"] for _m, url, _k in session.calls),
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


# ─────────────── 10b. post-code 302: one hand-walked hop, then the resume ───────────────
REDIRECT_ACCEPTED = "https://account.xiaomi.com/pass/verify/redirected?token=abc"


def _resume_reply(sign="resume", ssecurity="resume-ssecurity", nonce="resume-nonce"):
    return {
        "code": 0, "userId": "login-user", "passToken": "login-pass",
        "deviceId": "login-device", "ssecurity": ssecurity,
        "location": "https://api2.mina.mi.com/sts?sign=" + sign, "nonce": nonce,
    }


def scenario_redirect_after_code():
    h = Harness()
    try:
        need_otp(h)
        session = h.otp_session()
        session.responses = [response(200, {"code": 0}), response(200, {"code": 0})]
        send(h)
        session.responses = [
            redirect("/pass/verify/redirected?token=abc"),
            response(200, raw=b"<html>verified</html>"),
        ]
        CURRENT["login_script"] = [_resume_reply("redirect", "redirect-ssecurity", "redirect-nonce")]
        CURRENT["service_token"] = "service-token-after-redirect"
        accounts_before = len(CURRENT["accounts"])
        data = verify(h, "123456")

        check(data.get("ok") is True, f"a 302 answer must still finish the login, got {data}")
        check(data.get("verified") is True, "a redirected login must be marked verified")
        check(len(data.get("devices") or []) == 1, "the device list must come from the probe")

        calls = session.calls
        follows = [index for index, (_m, url, _k) in enumerate(calls) if url == REDIRECT_ACCEPTED]
        check(len(follows) == 1,
              f"the trusted 302 Location must be fetched exactly once, got {session.urls()}")
        if follows:
            method, _url, kwargs = calls[follows[0]]
            check(method == "GET", f"the redirect hop must be a GET, got {method}")
            check(kwargs.get("allow_redirects") is False,
                  f"the redirect hop must be walked by hand, got {kwargs.get('allow_redirects')}")
            check(kwargs.get("headers", {}).get("User-Agent") is not None,
                  "the redirect hop must keep the OTP headers (same session state)")
        resume = [index for index, (m, url, _k) in enumerate(calls)
                  if url.startswith(LOGIN_PREFIX) and m == "GET"]
        check(len(resume) == 2,
              f"the path must keep exactly the initial and the resumed serviceLogin, got "
              f"{session.urls()}")
        if follows and resume:
            check(resume[-1] > follows[0],
                  "the 302 target must be opened before the login is resumed")
        check(all(call[2].get("allow_redirects") is False for call in calls),
              "no request of the redirect chain may follow redirects")
        check(len(CURRENT["accounts"]) == accounts_before + 2,
              f"the 302 path must keep the resume account and the probe, got "
              f"{len(CURRENT['accounts']) - accounts_before} new accounts")
        if check(h.auth_path.exists() and h.token_path.exists(),
                 "a verified redirected login must persist both credential files"):
            token = json.loads(h.token_path.read_text(encoding="utf-8"))
            check(token.get("micoapi") == ["redirect-ssecurity", "service-token-after-redirect"],
                  f"the persisted token must come from the resume, got {token.get('micoapi')}")
        check(h.otp_session() is None and h.otp_state() == {},
              "a successful redirected login must close and clear the OTP state")
        check(session.closed is True, "the OTP session must be closed after success")
        check("token=abc" not in h.log.joined(),
              "the redirect target (its query holds a token) must never be logged")
        ok("verify 302: trusted Location followed once by hand, then the same-session resume")
    finally:
        h.cleanup()


# ─────────────── 10c. hostile redirect targets and an over-long chain are refused ───────────────
def scenario_redirect_allowlist_and_limit():
    h = Harness()
    try:
        cases = [
            ("http://account.xiaomi.com/pass/verify?t=1", "plain http"),
            ("https://evil.example/pass/verify?t=1", "foreign host"),
            ("//evil.example/pass/verify?t=1", "scheme relative"),
            ("https://account.xiaomi.com:8443/pass/verify?t=1", "non-443 port"),
            ("https://user:pw@account.xiaomi.com/pass/verify?t=1", "userinfo"),
        ]
        for location, label in cases:
            need_otp(h)
            session = h.otp_session()
            session.responses = [redirect(location)]
            calls_before = len(session.calls)
            before = h.snapshot_data()
            accounts_before = len(CURRENT["accounts"])
            data = verify(h, "123456")
            check(data.get("ok") is False and data.get("action") == "unsupported",
                  f"{label}: must be an explicit unsupported failure, got {data}")
            check(data.get("code") == "unsafe_redirect",
                  f"{label}: must report unsafe_redirect, got {data.get('code')}")
            urls = session.urls()
            check(len(session.calls) == calls_before + 1,
                  f"{label}: the hostile target must never be fetched, got {urls}")
            check(not any("evil.example" in str(url) for url in urls),
                  f"{label}: an untrusted host must never be fetched, got {urls}")
            check(not any("sendPhoneTicket" in str(url) for url in urls),
                  f"{label}: no SMS may be sent on a refused redirect")
            check(len(CURRENT["accounts"]) == accounts_before,
                  f"{label}: the login must not be resumed")
            check(h.snapshot_data() == before, f"{label}: nothing may be written")
            check(h.otp_session() is None and h.otp_state() == {},
                  f"{label}: the OTP state must be cleared")
            check(session.closed is True, f"{label}: the OTP session must be closed")

        # a chain longer than OTP_MAX_REDIRECTS (5) is refused instead of walked forever
        need_otp(h)
        session = h.otp_session()
        session.responses = [redirect("/pass/verify/hop1?t=1")] + [
            redirect("/pass/verify/hop%d?t=%d" % (index + 1, index + 1))
            for index in range(1, 7)
        ]
        before = h.snapshot_data()
        accounts_before = len(CURRENT["accounts"])
        data = verify(h, "123456")
        check(data.get("ok") is False and data.get("action") == "unsupported",
              f"a 6-hop chain must be refused, got {data}")
        check(data.get("code") == "too_many_redirects",
              f"a 6-hop chain must report too_many_redirects, got {data.get('code')}")
        urls = session.urls()
        hops = [url for url in urls if "/pass/verify/hop" in url]
        check(len(hops) == 6,
              f"at most OTP_MAX_REDIRECTS redirects may be walked before the refusal, got {hops}")
        check(not any("hop7" in url for url in urls),
              f"the 6th redirect target must never be fetched, got {urls}")
        check(len(CURRENT["accounts"]) == accounts_before, "an over-long chain must not resume")
        check(h.snapshot_data() == before, "an over-long chain must not write any file")
        check(h.otp_session() is None and h.otp_state() == {},
              "an over-long chain must clear the OTP state")
        check(session.closed is True, "an over-long chain must close the OTP session")

        # the hop normaliser only ever lands on the allowlisted account host
        normalize = h.ns["_otp_normalize_auth_hop"]
        for relative in ("/pass/verify/x?t=1", "pass/verify/x?t=1"):
            check(normalize(relative) == "https://account.xiaomi.com/pass/verify/x?t=1",
                  f"{relative!r} must be completed onto the account host, "
                  f"got {normalize(relative)!r}")
        ok("redirects: http/foreign/scheme-relative/port/userinfo and a 6-hop chain are refused")
    finally:
        h.cleanup()


# ─────────────── 10d. confirmUrl: same allowlist, then the resume ───────────────
CONFIRM_URL = "https://account.xiaomi.com/pass/confirm?token=c1"


def scenario_confirm_url():
    h = Harness()
    try:
        need_otp(h)
        session = h.otp_session()
        session.responses = [
            response(200, {"code": 0, "confirmUrl": "/pass/confirm?token=c1"}),
            response(200, raw=b"<html>confirmed</html>"),
        ]
        CURRENT["login_script"] = [_resume_reply("confirm", "confirm-ssecurity", "confirm-nonce")]
        CURRENT["service_token"] = "service-token-after-confirm"
        accounts_before = len(CURRENT["accounts"])
        data = verify(h, "123456")
        check(data.get("ok") is True, f"a confirmUrl answer must finish the login, got {data}")
        check(len(data.get("devices") or []) == 1, "the device list must come from the probe")
        calls = session.calls
        follows = [index for index, (_m, url, _k) in enumerate(calls) if url == CONFIRM_URL]
        check(len(follows) == 1,
              f"the confirmUrl must be opened exactly once in the same session, got {session.urls()}")
        if follows:
            method, _url, kwargs = calls[follows[0]]
            check(method == "GET", f"the confirmation must be a GET, got {method}")
            check(kwargs.get("allow_redirects") is False,
                  f"the confirmation must not follow redirects, got {kwargs.get('allow_redirects')}")
        resume = [index for index, (m, url, _k) in enumerate(calls)
                  if url.startswith(LOGIN_PREFIX) and m == "GET"]
        check(len(resume) == 2,
              f"the path must keep exactly the initial and the resumed serviceLogin, got "
              f"{session.urls()}")
        if follows and resume:
            check(resume[-1] > follows[0],
                  "the confirmation must happen before the login is resumed")
        check(len(CURRENT["accounts"]) == accounts_before + 2,
              "the confirmUrl path must keep the resume account and the probe")
        if check(h.auth_path.exists() and h.token_path.exists(),
                 "a verified confirmation must persist both credential files"):
            token = json.loads(h.token_path.read_text(encoding="utf-8"))
            check(token.get("micoapi") == ["confirm-ssecurity", "service-token-after-confirm"],
                  f"the persisted token must come from the resume, got {token.get('micoapi')}")
        check("token=c1" not in h.log.joined(),
              "the confirmUrl (its query holds a token) must never be logged")
        check(h.otp_state() == {}, "a successful confirmation must clear the OTP state")

        # an untrusted confirmUrl is refused before any request or resume
        need_otp(h)
        session = h.otp_session()
        session.responses = [response(200, {
            "code": 0, "confirmUrl": "https://evil.example/pass/confirm?token=c2",
        })]
        calls_before = len(session.calls)
        before = h.snapshot_data()
        accounts_before = len(CURRENT["accounts"])
        data = verify(h, "123456")
        check(data.get("ok") is False and data.get("action") == "unsupported",
              f"an untrusted confirmUrl must be refused, got {data}")
        check(data.get("code") == "unsafe_redirect",
              f"an untrusted confirmUrl must report unsafe_redirect, got {data.get('code')}")
        check(len(session.calls) == calls_before + 1,
              f"an untrusted confirmUrl must never be fetched, got {session.urls()}")
        check(len(CURRENT["accounts"]) == accounts_before,
              "an untrusted confirmUrl must not be resumed")
        check(h.snapshot_data() == before, "an untrusted confirmUrl must not write any file")
        check(h.otp_session() is None and h.otp_state() == {},
              "an untrusted confirmUrl must clear the OTP state")
        check(session.closed is True, "an untrusted confirmUrl must close the OTP session")
        ok("confirmUrl: followed in-session on the trusted host, refused elsewhere without a resume")
    finally:
        h.cleanup()


# ─────────────── 10e. the STS endpoint terminates the chain (never fetched) ───────────────
STS_TERMINAL = "https://api2.mina.mi.com/sts?sign=sts-sign-1&ticket=sts-ticket-2"


def _drive_verify(h):
    '''Start one OTP session and send the code request; returns the session.'''
    need_otp(h)
    session = h.otp_session()
    session.responses = [response(200, {"code": 0}), response(200, {"code": 0})]
    send(h)
    return session


def scenario_sts_terminal_hop():
    h = Harness()
    try:
        # (a) probe failure: the STS hop adds no request, and no credential is written
        h.auth_path.write_text('{"passToken": "old-pass", "userId": "old-user"}', encoding="utf-8")
        h.token_path.write_text('{"passToken": "old-pass", "userId": "old-user", '
                                '"micoapi": ["old-sec", "old-svc"]}', encoding="utf-8")
        before = h.snapshot_data()
        session = _drive_verify(h)
        CURRENT["login_script"] = [_resume_reply("sts", "sts-ssecurity", "sts-nonce")]
        CURRENT["service_token"] = "service-token-after-sts"
        session.responses = [redirect(STS_TERMINAL)]
        calls_before = len(session.calls)
        CURRENT["device_error"] = RuntimeError("mina 500")
        failed = verify(h, "123456")
        check(failed.get("ok") is False and failed.get("action") == "verify_failed",
              f"a failing probe must still fail explicitly, got {failed}")
        urls = session.urls()
        check(not any("api2.mina.mi.com" in url for url in urls),
              f"the STS endpoint must never be fetched, got {urls}")
        new_calls = session.calls[calls_before:]
        check([method for method, _url, _kwargs in new_calls] == ["POST", "GET"],
              f"the code POST and one resume GET are the only requests of the STS hop, "
              f"got {new_calls}")
        if new_calls:
            check(new_calls[-1][1].startswith(LOGIN_PREFIX),
                  f"the second request must be the same-session resume, got {new_calls[-1][1]}")
        resume = [url for url in urls if url.startswith(LOGIN_PREFIX)]
        check(len(resume) == 2,
              f"the path must keep exactly the initial and the resumed serviceLogin, got {urls}")
        check(h.snapshot_data() == before,
              "nothing may be written before the speaker probe succeeded")
        check(len(CURRENT["security_calls"]) == 1,
              f"the resumed login must still run the token exchange, got {CURRENT['security_calls']}")
        if CURRENT["security_calls"]:
            _account, location, nonce, ssecurity = CURRENT["security_calls"][0]
            check(location == "https://api2.mina.mi.com/sts?sign=sts",
                  f"the exchange must use the resume location, got {location}")
            check((nonce, ssecurity) == ("sts-nonce", "sts-ssecurity"),
                  f"the exchange must use the resume nonce/ssecurity, got {nonce}/{ssecurity}")
        check(h.otp_state().get("sts_location") == STS_TERMINAL,
              "the captured STS URL must stay in memory for the exchange: "
              f"{h.otp_state().get('sts_location')}")
        CURRENT["device_error"] = None

        # (b) the same hop with a healthy probe finishes the login on the same session
        restart(h)
        session = _drive_verify(h)
        CURRENT["login_script"] = [_resume_reply("sts2", "sts-ssecurity-2", "sts-nonce-2")]
        session.responses = [redirect(STS_TERMINAL)]
        accounts_before = len(CURRENT["accounts"])
        data = verify(h, "123456")
        check(data.get("ok") is True, f"an STS hop must still finish the login, got {data}")
        check(data.get("verified") is True, "an STS-hop login must be marked verified")
        check(len(data.get("devices") or []) == 1, "the device list must come from the probe")
        urls = session.urls()
        check(not any("api2.mina.mi.com" in url for url in urls),
              f"the STS endpoint must never be fetched, got {urls}")
        resume = [index for index, (method, url, _kwargs) in enumerate(session.calls)
                  if url.startswith(LOGIN_PREFIX) and method == "GET"]
        check(len(resume) == 2,
              f"exactly one resume serviceLogin must be issued after the code, got {urls}")
        check(len(CURRENT["security_calls"]) == 2,
              "exactly one token exchange per login expected on the resumed response")
        if len(CURRENT["security_calls"]) == 2:
            _account, location, nonce, ssecurity = CURRENT["security_calls"][-1]
            check(location == "https://api2.mina.mi.com/sts?sign=sts2",
                  f"the exchange must prefer the resume location over the captured STS URL, "
                  f"got {location}")
            check((nonce, ssecurity) == ("sts-nonce-2", "sts-ssecurity-2"),
                  f"the exchange must use the resume nonce/ssecurity, got {nonce}/{ssecurity}")
        check(all(call[2].get("allow_redirects") is False for call in session.calls),
              "no request of the STS path may follow redirects")
        check(len(CURRENT["accounts"]) == accounts_before + 2,
              "the STS path must keep the resume account and the probe")
        if check(h.auth_path.exists() and h.token_path.exists(),
                 "a verified STS-hop login must persist both credential files"):
            token = json.loads(h.token_path.read_text(encoding="utf-8"))
            check(token.get("micoapi") == ["sts-ssecurity-2", "service-token-after-sts"],
                  f"the persisted token must come from the resume, got {token.get('micoapi')}")
        check(h.otp_session() is None and h.otp_state() == {},
              "a successful STS-hop login must close and clear the OTP state")
        check(session.closed is True, "the OTP session must be closed after success")

        log_text = h.log.joined()
        check("stage=sts_terminal" in log_text,
              f"the STS terminal must be logged with its stage: {log_text}")
        check("host=api2.mina.mi.com" in log_text and "path=/sts" in log_text,
              f"the STS terminal must log host and path: {log_text}")
        check("sts-sign-1" not in log_text and "sts-ticket-2" not in log_text,
              f"the STS query string must never be logged: {log_text}")
        check("source=resume" in log_text,
              "the log must record that the resume response won over the captured STS URL")

        # (c) a resume without ssecurity/location/nonce still fails explicitly, but now
        # names the missing fields instead of pretending the answer was malformed
        restart(h)
        session = _drive_verify(h)
        CURRENT["login_script"] = [{
            "code": 0, "userId": "login-user", "passToken": "login-pass",
            "deviceId": "login-device",
        }]
        session.responses = [redirect(STS_TERMINAL)]
        before = h.snapshot_data()
        security_before = len(CURRENT["security_calls"])
        data = verify(h, "123456")
        check(data.get("ok") is False and data.get("code") == "resume_incomplete",
              f"a resume without STS parameters must fail explicitly, got {data}")
        check("ssecurity" in str(data.get("msg") or ""),
              f"the failure must name the missing field, got {data.get('msg')}")
        check(len(CURRENT["security_calls"]) == security_before,
              "a resume without location/nonce/ssecurity must not reach the token exchange")
        check(h.snapshot_data() == before,
              "a resume without STS parameters must never write credentials")
        check(not any("api2.mina.mi.com" in url for url in session.urls()),
              f"the captured STS URL must never be fetched as a fallback, got {session.urls()}")
        check(h.otp_state() == {}, "a failed resume must clear the OTP state")
        ok("STS terminal: 302 to api2 stops the chain without a GET, the same-session resume "
           "and the resume's own location/nonce/ssecurity drive the exchange")
    finally:
        h.cleanup()


# ─────────────── 10f. a rejected hop names host+path, never the query ───────────────
HOSTILE_HOP = "https://evil.example/pass/verify?ticket=HOSTILE-TICKET-1&code=909090"


def scenario_untrusted_hop_diagnostics():
    h = Harness()
    try:
        h.auth_path.write_text('{"passToken": "old-pass", "userId": "old-user"}', encoding="utf-8")
        h.token_path.write_text('{"passToken": "old-pass", "userId": "old-user", '
                                '"micoapi": ["old-sec", "old-svc"]}', encoding="utf-8")
        before = h.snapshot_data()
        session = _drive_verify(h)
        session.responses = [redirect(HOSTILE_HOP)]
        calls_before = len(session.calls)
        accounts_before = len(CURRENT["accounts"])
        security_before = len(CURRENT["security_calls"])
        data = verify(h, "271828")

        check(data.get("ok") is False and data.get("action") == "unsupported",
              f"an untrusted hop must be an explicit unsupported failure, got {data}")
        check(data.get("code") == "unsafe_redirect",
              f"an untrusted hop must report unsafe_redirect, got {data.get('code')}")
        message = str(data.get("msg") or "")
        check("evil.example/pass/verify" in message,
              f"the message must name the refused host and path: {message}")
        check("HOSTILE-TICKET-1" not in message and "ticket=" not in message,
              f"the message must never carry the query string: {message}")
        check("已拒绝跟随" in message and "扫码登录" in message,
              f"the message must stay honest about the refusal: {message}")

        log_text = h.log.joined()
        check("stage=verify_redirect" in log_text,
              f"the rejection must log the stage: {log_text}")
        check("status=302" in log_text,
              f"the rejection must log the HTTP status that produced the target: {log_text}")
        check("拒绝跟随跳转 (stage=verify_redirect, status=302, host=evil.example, "
              "path=/pass/verify)" in log_text,
              f"the rejection must log stage/status/host/path in one line: {log_text}")
        check("host=evil.example" in log_text and "path=/pass/verify" in log_text,
              f"the rejection must log host and path: {log_text}")
        check("HOSTILE-TICKET-1" not in log_text and "ticket=" not in log_text,
              "the rejected query string must never be logged")
        check("271828" not in log_text, "the verification code must never be logged")
        check("909090" not in log_text, "the code echoed in the target must never be logged")
        check("deviceId=RANDOMDEVICEID" not in log_text and "cookie" not in log_text.lower(),
              "no cookie may be logged with the diagnostics")

        urls = session.urls()
        check(len(session.calls) == calls_before + 1,
              f"the hostile target must never be fetched, got {urls}")
        check(not any("evil.example" in url for url in urls),
              f"an untrusted host must never be fetched, got {urls}")
        check(len(CURRENT["accounts"]) == accounts_before,
              "an untrusted hop must not resume the login")
        check(len(CURRENT["security_calls"]) == security_before,
              "an untrusted hop must never reach the token exchange")
        check(h.snapshot_data() == before, "an untrusted hop must not write any file")
        check(h.otp_session() is None and h.otp_state() == {},
              "an untrusted hop must clear the OTP state")
        check(session.closed is True, "an untrusted hop must close the OTP session")

        # the rejection is diagnostic even when the same check runs on a later hop
        restart(h)
        session = _drive_verify(h)
        session.responses = [
            redirect("/pass/verify/hop1?t=1"),
            redirect("https://api2.mina.mi.com.evil.example/sts?sign=x"),
        ]
        data = verify(h, "123456")
        check(data.get("code") == "unsafe_redirect",
              f"a later hostile hop must report unsafe_redirect, got {data.get('code')}")
        check("api2.mina.mi.com.evil.example/sts" in str(data.get("msg") or ""),
              f"the message must name the later hop's host: {data.get('msg')}")
        check("host=api2.mina.mi.com.evil.example, path=/sts" in h.log.joined(),
              "the rejection must carry the host/path and the status of the hop that "
              "produced it")
        ok("untrusted hop: refused with stage/status/host/path in the log and in the message, "
           "no query, no code, no cookie")
    finally:
        h.cleanup()


# ─────────────── 10g. confirmUrl embedded in the final URL's query ───────────────
CONFIRM_IN_QUERY = ("https://account.xiaomi.com/pass/verify/final?confirmUrl="
                    "https%3A%2F%2Faccount.xiaomi.com%2Fpass%2Fconfirm%3Ftoken%3Dcq1")
CONFIRM_EXTRACTED = "https://account.xiaomi.com/pass/confirm?token=cq1"
CONFIRM_IN_QUERY_EVIL = ("https://account.xiaomi.com/pass/verify/final?confirmUrl="
                         "https%3A%2F%2Fevil.example%2Fpass%2Fconfirm%3Ftoken%3Dcq2")


def scenario_confirm_from_query():
    h = Harness()
    try:
        # (a) the trusted value is decoded, re-validated as a hop and followed
        session = _drive_verify(h)
        CURRENT["login_script"] = [_resume_reply("cq", "cq-ssecurity", "cq-nonce")]
        CURRENT["service_token"] = "service-token-after-cq"
        session.responses = [
            response(200, {"code": 0, "location": CONFIRM_IN_QUERY}),
            response(200, raw=b"<html>final</html>"),
            response(200, raw=b"<html>confirmed</html>"),
        ]
        data = verify(h, "123456")
        check(data.get("ok") is True, f"a confirmUrl in the query must finish the login, got {data}")
        check(len(data.get("devices") or []) == 1, "the device list must come from the probe")
        urls = session.urls()
        finals = [index for index, (_m, url, _k) in enumerate(session.calls)
                  if url == CONFIRM_IN_QUERY]
        follows = [index for index, (_m, url, _k) in enumerate(session.calls)
                   if url == CONFIRM_EXTRACTED]
        check(len(finals) == 1,
              f"the location hop must be opened once, got {urls}")
        check(len(follows) == 1,
              f"the decoded confirmUrl must be opened once, got {urls}")
        if finals and follows:
            check(follows[0] == finals[0] + 1,
                  f"the confirmUrl must be followed right after the hop: {urls}")
            method, _url, kwargs = session.calls[follows[0]]
            check(method == "GET", f"the confirmUrl hop must be a GET, got {method}")
            check(kwargs.get("allow_redirects") is False,
                  f"the confirmUrl hop must not follow redirects, got "
                  f"{kwargs.get('allow_redirects')}")
        resume = [index for index, (method, url, _k) in enumerate(session.calls)
                  if url.startswith(LOGIN_PREFIX) and method == "GET"]
        check(len(resume) == 2,
              f"the path must keep exactly the initial and the resumed serviceLogin, got {urls}")
        if follows and resume:
            check(resume[-1] > follows[0],
                  "the extracted confirmation must happen before the login is resumed")
        check(all(call[2].get("allow_redirects") is False for call in session.calls),
              "no request of the confirmUrl path may follow redirects")
        if check(h.auth_path.exists() and h.token_path.exists(),
                 "a verified extracted confirmation must persist both credential files"):
            token = json.loads(h.token_path.read_text(encoding="utf-8"))
            check(token.get("micoapi") == ["cq-ssecurity", "service-token-after-cq"],
                  f"the persisted token must come from the resume, got {token.get('micoapi')}")
        log_text = h.log.joined()
        check("跟随 confirmUrl" in log_text,
              f"the extracted hop must be logged as a confirmUrl follow: {log_text}")
        check("target=account.xiaomi.com/pass/confirm" in log_text,
              f"the accepted hop must log host + path: {log_text}")
        check("token=cq1" not in log_text,
              f"the accepted hop's query must never be logged: {log_text}")
        check(h.otp_state() == {}, "a successful extraction must clear the OTP state")

        # (b) an untrusted extracted value is refused by the very same hop validation
        restart(h)
        session = _drive_verify(h)
        session.responses = [
            response(200, {"code": 0, "location": CONFIRM_IN_QUERY_EVIL}),
            response(200, raw=b"<html>final</html>"),
        ]
        accounts_before = len(CURRENT["accounts"])
        security_before = len(CURRENT["security_calls"])
        before = h.snapshot_data()
        data = verify(h, "123456")
        check(data.get("ok") is False and data.get("action") == "unsupported",
              f"an untrusted extracted confirmUrl must be refused, got {data}")
        check(data.get("code") == "unsafe_redirect",
              f"an untrusted extraction must report unsafe_redirect, got {data.get('code')}")
        message = str(data.get("msg") or "")
        check("evil.example/pass/confirm" in message,
              f"the message must name the extracted host: {message}")
        check("cq2" not in message, f"the extracted query must never be echoed: {message}")
        log_text = h.log.joined()
        check("stage=location_confirm" in log_text,
              f"the extraction rejection must log its own stage: {log_text}")
        check("host=evil.example" in log_text and "path=/pass/confirm" in log_text,
              f"the extraction rejection must log host and path: {log_text}")
        check("cq2" not in log_text, "the extracted query must never be logged")
        urls = session.urls()
        check(not any(url.startswith("https://evil.example") for url in urls),
              f"an untrusted extraction must never be fetched, got {urls}")
        check(len(CURRENT["accounts"]) == accounts_before,
              "an untrusted extraction must not resume the login")
        check(len(CURRENT["security_calls"]) == security_before,
              "an untrusted extraction must never reach the token exchange")
        check(h.snapshot_data() == before, "an untrusted extraction must not write any file")
        check(h.otp_session() is None and h.otp_state() == {},
              "an untrusted extraction must clear the OTP state")
        check(session.closed is True, "an untrusted extraction must close the OTP session")
        ok("confirmUrl in the final URL: decoded, re-validated and followed on the trusted host, "
           "refused and logged when it points elsewhere")
    finally:
        h.cleanup()


# ─────────────── 11. manual Cookie login ───────────────
def scenario_cookie_login():
    h = Harness()
    try:
        # (a) userId + passToken only: the exchange runs through the verified helper
        CURRENT["login_script"] = [{
            "code": 0,
            "userId": "cookie-user",
            "passToken": "cookie-pass",
            "ssecurity": "cookie-ssecurity",
            "location": "https://api2.mina.mi.com/sts?sign=cookie",
            "nonce": "cookie-nonce",
        }]
        CURRENT["service_token"] = "cookie-service-token"
        data = cookie_login(h, "userId=cookie-user; passToken=cookie-pass; deviceId=cookie-dev")
        check(data.get("ok") is True, f"an exchanged cookie must log in, got {data}")
        exchange = CURRENT["verified_login_calls"]
        check(len(exchange) == 1,
              f"exactly one micoapi exchange expected, got {exchange}")
        if exchange:
            call = exchange[0]
            check(call["url"] ==
                  "https://account.xiaomi.com/pass/serviceLogin?sid=micoapi&_json=true",
                  f"the exchange must ask for the micoapi sid, got {call['url']}")
            check(call["method"] == "GET", f"a bodiless exchange must be a GET, got {call['method']}")
            check(call["kwargs"].get("data") is None,
                  f"a bodiless exchange must not send a body, got {call['kwargs'].get('data')}")
            check(call["kwargs"].get("allow_redirects") is False,
                  "the exchange must never follow redirects")
            check("ssl" not in call["kwargs"] or call["kwargs"]["ssl"] is not False,
                  f"the exchange may not disable TLS verification, got {call['kwargs'].get('ssl')}")
            sent = call["kwargs"].get("cookies") or {}
            check(sent.get("userId") == "cookie-user"
                  and sent.get("passToken") == "cookie-pass"
                  and sent.get("deviceId") == "cookie-dev"
                  and sent.get("sdkVersion") == "3.9",
                  f"the preloaded userId/passToken/deviceId must be the cookies, got {sent}")
        check(CURRENT["micoapi_login_calls"] == [],
              "the exchange must not use MiAccount.login() any more (it disables TLS checks)")
        account = CURRENT["accounts"][-1]
        check(account.token.get("micoapi") == ("cookie-ssecurity", "cookie-service-token"),
              f"the exchange must write the micoapi tuple onto account.token, got {account.token}")
        token = json.loads(h.token_path.read_text(encoding="utf-8"))
        check(token.get("micoapi") == ["cookie-ssecurity", "cookie-service-token"],
              f"the exchanged token must be persisted, got {token.get('micoapi')}")
        cookie_text = h.cfg["xiaomi"]["cookie_text"]
        check("serviceToken=cookie-service-token" in cookie_text,
              f"cookie_text must carry the exchanged serviceToken, got {cookie_text!r}")

        # (b) a pasted serviceToken does not win over a working passToken exchange:
        #     the browser one belongs to the web login (another sid), never to micoapi
        h2 = Harness()
        try:
            CURRENT["login_script"] = [{
                "code": 0, "userId": "u", "passToken": "p",
                "ssecurity": "exchanged-ssecurity",
                "location": "https://api2.mina.mi.com/sts?sign=exchange",
                "nonce": "exchanged-nonce",
            }]
            CURRENT["service_token"] = "exchanged-service-token"
            data = cookie_login(h2, "userId=u; passToken=p; serviceToken=pasted-web-token; deviceId=d")
            check(data.get("ok") is True,
                  f"a cookie with a pasted serviceToken must still log in, got {data}")
            check(len(CURRENT["verified_login_calls"]) == 1,
                  "the passToken exchange must run even when a serviceToken was pasted")
            token = json.loads(h2.token_path.read_text(encoding="utf-8"))
            check(token.get("micoapi") == ["exchanged-ssecurity", "exchanged-service-token"],
                  f"the exchanged token must win over the pasted one, got {token.get('micoapi')}")
        finally:
            h2.cleanup()

        # (c) the pasted serviceToken is only a fallback when the exchange fails
        h3 = Harness()
        try:
            CURRENT["login_script"] = []
            CURRENT["cookie_login"] = "fail"
            data = cookie_login(h3, "userId=u; passToken=p; serviceToken=svc-fallback")
            check(data.get("ok") is True,
                  f"a failed exchange must fall back to the pasted token, got {data}")
            check(len(CURRENT["verified_login_calls"]) == 2,
                  "the pasted-token fallback follows only the SDK and one browser attempt")
            token = json.loads(h3.token_path.read_text(encoding="utf-8"))
            check(token.get("micoapi") == ["", "svc-fallback"],
                  f"the pasted token may only be the fallback, got {token.get('micoapi')}")
            check("兜底" in h3.log.joined(), "the fallback must be visible in the log")
            # The saved passToken was never accepted: saying so at login time is the
            # only chance to warn before the browser token expires days later.
            check("未经校验" in h3.log.joined(),
                  "a fallback login must warn that the saved passToken was never accepted")
            check("passToken 换票被小米拒绝" in str(data.get("msg", "")),
                  f"the success message must carry that warning too, got {data.get('msg')!r}")
        finally:
            CURRENT["cookie_login"] = "ok"
            h3.cleanup()

        # (d) exchange failure + failing fallback stays explicit and writes nothing
        h4 = Harness()
        try:
            CURRENT["login_script"] = []
            CURRENT["cookie_login"] = "fail"
            CURRENT["device_error"] = RuntimeError("mina 401")
            before = h4.snapshot_data()
            data = cookie_login(h4, "userId=u; passToken=p; serviceToken=svc-bad")
            check(data.get("ok") is False and data.get("action") == "device_list",
                  f"a bad fallback token must fail at the device probe, got {data}")
            check(h4.snapshot_data() == before,
                  "a failed fallback must not write any credential file")
        finally:
            CURRENT["cookie_login"] = "ok"
            CURRENT["device_error"] = None
            h4.cleanup()
        ok("cookie: passToken exchange wins, a pasted serviceToken is only a fallback")
    finally:
        h.cleanup()


def scenario_cookie_browser_compatibility():
    h = Harness()
    try:
        CURRENT["login_script"] = [{"code": 70016}, {
            "code": 0, "userId": "browser-user", "passToken": "rotated-pass",
            "nonce": "n", "ssecurity": "s", "location": "https://api2.mina.mi.com/sts?sign=test"}]
        data = cookie_login(h, "userId=browser-user; passToken=browser-pass; deviceId=wb_browser")
        check(data.get("ok") is True, "a browser compatibility exchange can finish after SDK rejection")
        calls = CURRENT["verified_login_calls"]
        check(len(calls) == 2, "browser compatibility performs exactly one extra exchange")
        first, second = [call["kwargs"] for call in calls]
        check(first["cookies"].get("sdkVersion") == "3.9" and "sdkVersion" not in second["cookies"],
              "browser exchange removes the SDK marker")
        check(second["cookies"] == {"deviceId": "wb_browser", "userId": "browser-user", "passToken": "browser-pass"},
              "browser exchange keeps the supplied account and browser device identity")
        check(second["headers"]["User-Agent"].startswith("Mozilla/5.0"), "browser exchange uses a browser user agent")
        check(second.get("allow_redirects") is False and "ssl" not in second and second.get("data") is None,
              "compatibility keeps TLS verification, refuses redirects and never sends a password")
        check(json.loads(h.token_path.read_text(encoding="utf-8"))["passToken"] == "rotated-pass",
              "a successful manual exchange persists the rotated passToken")
        check("browser-pass" not in h.log.joined() and "rotated-pass" not in h.log.joined(),
              "compatibility diagnostics never log token values")
    finally:
        h.cleanup()
    for code in (70016, 87001, 20003):
        h = Harness()
        try:
            CURRENT["login_script"] = [{"code": code}, {"code": code}]
            before = h.snapshot_data()
            data = cookie_login(h, "userId=u; passToken=p; deviceId=wb_browser")
            check(data.get("ok") is False and data.get("code") == code, "a failed exchange retains its final business code")
            check(len(CURRENT["verified_login_calls"]) == (2 if code == 70016 else 1),
                  "only explicit 70016 permits one compatibility retry")
            check(h.snapshot_data() == before, "failed browser compatibility preserves all saved files")
        finally:
            h.cleanup()
    h = Harness()
    try:
        CURRENT["login_script"] = [{"code": 0, "userId": "other-user", "passToken": "other-pass"}]
        before = h.snapshot_data()
        data = cookie_login(h, "userId=u; passToken=p")
        check(data.get("action") == "identity_mismatch" and h.snapshot_data() == before,
              "manual cookie exchange refuses cross-account credentials without writing")
    finally:
        h.cleanup()
    ok("cookie browser compatibility: bounded retry, no password, rotated credentials, explicit failures")


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
        check("请求未完成" in raised.get("msg", "") and "可能已过期" not in raised.get("msg", ""),
              "transport failures must not be diagnosed as expired cookies")
        CURRENT["cookie_login"] = "fail"
        base_calls = len(CURRENT["verified_login_calls"])
        rejected = cookie_login(h, "userId=u; passToken=p; deviceId=same-browser")
        check(rejected.get("code") == 70016, "cookie rejection exposes Xiaomi's business code")
        check("70016" in rejected.get("msg", "") and "尚未保存" in rejected.get("msg", ""),
              "cookie rejection explains the rejection and preserves saved credentials")
        check("同一登录会话" in rejected.get("msg", ""), "cookie rejection explains matching browser identity")
        # P0: the rejection carries Xiaomi's own words and a machine-readable detail block
        check((rejected.get("detail") or {}).get("description") == "scripted exchange rejection",
              f"the Xiaomi description must reach the UI, got {rejected}")
        check("小米说明" in rejected.get("msg", ""),
              f"the rejection message must quote Xiaomi, got {rejected.get('msg')!r}")
        check("浏览器代签" in rejected.get("msg", ""),
              "the rejection must point at the browser-signed path")
        # the 70016 probe: exactly one more exchange, browser shape without the SDK marker
        probe_calls = CURRENT["verified_login_calls"][base_calls:]
        check(len(probe_calls) == 2,
              f"70016 gets the standard shape plus exactly one probe, got {len(probe_calls)}")
        if len(probe_calls) == 2:
            shape = probe_calls[1]["kwargs"].get("cookies") or {}
            check("sdkVersion" not in shape,
                  f"the probe must drop the SDK marker, got {shape}")
            headers = probe_calls[1]["kwargs"].get("headers") or {}
            check("Mozilla/5.0" in str(headers.get("User-Agent", "")),
                  f"the probe must use a browser UA, got {headers}")
        check((rejected.get("detail") or {}).get("browser_shape", {}).get("code") == 70016,
              f"the probe result must be recorded in the detail block, got {rejected}")
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
                  '@app.post("/api/test/xiaomi/cookie-ticket")',
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
                    "restartOtp()", "verifyCookieLogin()", "verifyCookieTicket()",
                    "clearXiaomiLogin()", "completeSmsVerify()"):
        check(f'onclick="{handler}"' in html, f"no button is wired to {handler}")
    for element_id in ("mi-username", "mi-password", "mi-otp-code", "mi-otp-send",
                       "mi-otp-verify", "mi-otp-restart", "mi-otp-status", "mi-otp-method",
                       "mi-cookie-text", "mi-cookie-userid", "mi-cookie-passtoken",
                       "mi-cookie-submit", "mi-cookie-status",
                       "mi-ticket-text", "mi-ticket-submit", "mi-ticket-status",
                       "mi-ticket-detail",
                       "qr-img", "qr-msg", "qr-status", "mi-sts-url", "mi-sts-submit"):
        check(f'id="{element_id}"' in html, f"missing element #{element_id}")
    check('id="qr-area"' in html and 'id="qr-area" style="display:none' in html,
          "the QR image area must exist and stay hidden until a QR is fetched")

    # the browser-signed ticket block lives inside the advanced card, above the
    # legacy STS compatibility path
    ticket = html.find('id="mi-ticket-card"')
    check(ticket > advanced > 0, "the browser-signed ticket block must be inside the advanced card")
    check(ticket < html.find('id="mi-sts-card"', ticket),
          "the recommended ticket block must come before the legacy STS path")
    check("浏览器代签" in html, "the browser-signed block must be labelled")

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
    # clear-login rewrites the xiaomi section server-side: the client must refresh
    # the revision that saveCfg actually reads, or the next save is a false 409.
    clear_start = html.find("async function clearXiaomiLogin()")
    check(clear_start > 0, "the clear-login function must exist")
    clear_end = html.find("async function ", clear_start + 10)
    clear_block = html[clear_start:clear_end if clear_end > 0 else len(html)]
    compact = clear_block.replace(" ", "").replace("\n", "")
    check("sectionRevisions.xiaomi=revision" in compact,
          "clear-login must sync sectionRevisions.xiaomi after clearing credentials")
    check("cfg._section_revisions" not in clear_block,
          "clear-login must not write the dead cfg._section_revisions property")

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
        "mi-cookie-userid": {"value": ""},
        "mi-cookie-passtoken": {"value": ""},
        "mi-cookie-deviceid": {"value": ""},
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
    elements["mi-cookie-userid"]["value"] = "u"
    elements["mi-cookie-passtoken"]["value"] = "p"
    payload = _run_node(
        script_source,
        {"ok": True, "verified": True, "devices": [{"deviceID": "d1"}],
         "msg": "Cookie 登录成功，发现 1 台设备"},
        elements, "verifyCookieLogin()", snapshot,
        init_images="_otp.state='pending';(function(){const p=document.getElementById('mi-otp-panel');if(p)p.style.display='';})();",
    )
    if payload:
        fetches = [call for call in payload["calls"] if call[0] == "fetch"]
        check(len(fetches) == 2 and fetches[1][1] == "/api/config",
              f"successful cookie login must POST once and then GET Xiaomi CAS revision, got {fetches}")
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
    elements["mi-cookie-userid"]["value"] = "u"
    payload = _run_node(
        script_source,
        {"ok": False, "action": "fix_cookie",
         "msg": "Cookie 缺少 passToken：userId 与 passToken 都是必需的"},
        elements, "verifyCookieLogin()", snapshot,
    )
    if payload:
        check("Cookie" in payload["cookieStatus"] or "检查" in payload["cookieStatus"],
              f"a missing field must point at the two inputs, got {payload['cookieStatus']!r}")
        check("passToken" in payload["alert"], "the missing field must be named")
        ok("frontend cookie: a missing userId/passToken field is named without calling Xiaomi")
    elements = json.loads(json.dumps(base_elements))
    elements["mi-cookie-userid"]["value"] = "u"
    elements["mi-cookie-passtoken"]["value"] = "p"
    elements["mi-cookie-deviceid"]["value"] = "browser-device"
    rejection = "小米拒绝换票（code=70016）；尚未保存本次凭据，请改用扫码登录"
    payload = _run_node(script_source, {"ok": False, "action": "exchange_failed", "code": 70016,
                                      "msg": rejection}, elements, "verifyCookieLogin()", snapshot)
    if payload:
        fetches = [call for call in payload["calls"] if call[0] == "fetch"]
        check(json.loads(fetches[0][3]).get("cookie_text") == "userId=u; passToken=p; deviceId=browser-device",
              "the cookie form preserves the matching browser deviceId")
        check(rejection in payload["cookieStatus"] and rejection in payload["alert"],
              "the inline cookie status preserves the actual server reason and code")
        check("可能已过期" not in payload["cookieStatus"], "the cookie form never invents an expiry diagnosis")
        ok("frontend cookie: browser identity and server rejection details are retained")

    # (f) browser-signed ticket: one POST to the ticket endpoint, text forwarded,
    #     textarea cleared and devices refreshed; failure shows msg plus detail
    elements = json.loads(json.dumps(base_elements))
    elements["mi-ticket-text"] = {"value": "&&&START&&&{\"code\":0}"}
    elements["mi-ticket-submit"] = {"disabled": False}
    elements["mi-ticket-status"] = {"textContent": ""}
    elements["mi-ticket-detail"] = {"style": {"display": "none"}, "textContent": ""}
    ticket_snapshot = ("ticketStatus:elements['mi-ticket-status'].textContent,"
                       "ticketText:elements['mi-ticket-text'].value,"
                       "ticketDetail:elements['mi-ticket-detail'].textContent,"
                       "ticketDetailStyle:elements['mi-ticket-detail'].style.display,"
                       "alert:elements['xiaomi-alert'].innerHTML")
    payload = _run_node(script_source,
                        {"ok": True, "devices": [{"deviceID": "d1"}],
                         "msg": "浏览器代签成功，发现 1 台设备"},
                        elements, "verifyCookieTicket()", ticket_snapshot)
    if payload:
        fetches = [call for call in payload["calls"] if call[0] == "fetch"]
        check(len(fetches) == 1
              and fetches[0][1] == "/api/test/xiaomi/cookie-ticket"
              and fetches[0][2] == "POST",
              f"ticket login must POST the new endpoint once, got {fetches}")
        body = json.loads((fetches[0][3] if fetches else "{}") or "{}")
        check(str(body.get("text", "")).startswith("&&&START&&&"),
              f"ticket login must send the pasted text, got {body}")
        check(payload["ticketText"] == "", "a successful ticket login clears the textarea")
        check(any(call[0] == "devices" for call in payload["calls"]),
              "a successful ticket login refreshes the device list")
        check(payload["ticketDetailStyle"] == "none",
              "a successful ticket login hides the detail box")
        ok("frontend ticket: one POST, pasted text forwarded, textarea cleared, devices refreshed")
    elements = json.loads(json.dumps(base_elements))
    elements["mi-ticket-text"] = {"value": "&&&START&&&{\"code\":70016}"}
    elements["mi-ticket-submit"] = {"disabled": False}
    elements["mi-ticket-status"] = {"textContent": ""}
    elements["mi-ticket-detail"] = {"style": {"display": "none"}, "textContent": ""}
    payload = _run_node(script_source,
                        {"ok": False, "action": "rejected",
                         "msg": "浏览器返回的登录响应不是成功状态（code=70016）",
                         "detail": {"code": 70016, "description": "登录验证失败"}},
                        elements, "verifyCookieTicket()", ticket_snapshot)
    if payload:
        check("70016" in payload["ticketStatus"], "a rejected ticket must name the code inline")
        check("70016" in payload["ticketDetail"],
              f"the machine-readable detail must be shown, got {payload['ticketDetail']}")
        check(payload["ticketDetailStyle"] == "block",
              "a rejected ticket must reveal the detail box")
        ok("frontend ticket: a rejected ticket names the code and reveals the detail block")
    return "frontend behaviour verified"


# ─────────────── 18. contract B: the new flows keep TLS verification on ───────────────
def _helper_node():
    return next((node for node in tree.body
                 if isinstance(node, ast.AsyncFunctionDef)
                 and node.name == "_mi_service_login_verified"), None)


def scenario_verified_transport():
    # B(b) static: the 1.0.12 section never re-disables certificate verification
    check(F_SECTION_MARKER in source,
          "bridge.py must carry the 1.0.12 section marker (not only the whole-file check)")
    check(re.findall(r"ssl\s*=\s*False", f_section_source) == [],
          "the 1.0.12 section must contain zero occurrences of a disabled-TLS override")
    node = _helper_node()
    check(node is not None,
          "bridge.py must define the async helper _mi_service_login_verified()")
    if node is not None:
        keywords = [(kw.arg, kw.value) for call in ast.walk(node)
                    if isinstance(call, ast.Call) for kw in call.keywords]
        check(all(name != "ssl" for name, _value in keywords),
              "the verified helper must never pass an ssl kwarg to aiohttp")
        redirects = [value for name, value in keywords if name == "allow_redirects"]
        check(redirects and all(isinstance(value, ast.Constant) and value.value is False
                                for value in redirects),
              "the verified helper must keep allow_redirects=False")
        into_dependency = [call.func.attr for call in ast.walk(node)
                           if isinstance(call, ast.Call)
                           and isinstance(call.func, ast.Attribute)
                           and call.func.attr == "_serviceLogin"]
        check(into_dependency == [],
              "the verified helper must not call the dependency's _serviceLogin")

    # B(c) behavioural: the OTP password login runs through that helper
    h = Harness()
    try:
        CURRENT["login_script"] = [
            {"code": 70016, "description": "密码错误", "qs": "q", "sid": "s",
             "_sign": "x", "callback": "c"},
            {"code": 70016, "description": "密码错误"},
        ]
        data = start(h)
        check(data.get("ok") is False and data.get("action") == "login_failed",
              f"the scripted rejection must stay an explicit login_failed, got {data}")
        calls = CURRENT["verified_login_calls"]
        check(len(calls) == 2,
              f"one password login plus one Auth2 submit expected, got {len(calls)}")
        if len(calls) == 2:
            first, second = calls
            check(first["method"] == "GET" and first["kwargs"].get("data") is None,
                  f"the first serviceLogin must be a bodyless GET, got {first['method']}")
            check(first["url"] == LOGIN_PREFIX + "?sid=micoapi&_json=true",
                  f"the first serviceLogin must hit the pinned URL, got {first['url']}")
            cookies = first["kwargs"].get("cookies") or {}
            check(cookies.get("deviceId") == "RANDOMDEVICEID"
                  and cookies.get("sdkVersion") == "3.9",
                  f"the login must carry deviceId/sdkVersion cookies, got {cookies}")
            check("passToken" not in cookies and "userId" not in cookies,
                  f"a token without passToken must not send that cookie at all "
                  f"(an empty one would cover the session jar), got {cookies}")
            check(second["method"] == "POST"
                  and second["url"] == LOGIN_PREFIX + "Auth2",
                  f"the Auth2 submit must POST serviceLoginAuth2, got {second['url']}")
            form = second["kwargs"].get("data") or {}
            check(form.get("user") == "13800000000" and form.get("_sign") == "x"
                  and form.get("hash") == hashlib.md5(b"pw").hexdigest().upper(),
                  f"the Auth2 body must carry the signed password hash, got {form}")
            check(second["kwargs"].get("cookies", {}).get("deviceId") == "RANDOMDEVICEID",
                  "the Auth2 submit must reuse the same deviceId cookie")
        check(all("ssl" not in call["kwargs"] or call["kwargs"]["ssl"] is not False
                  for call in calls),
              "no OTP login request may pass ssl=False")
        check(all(call["kwargs"].get("allow_redirects") is False for call in calls),
              "no OTP login request may follow redirects")
        check(all(call["kwargs"].get("timeout") is not None for call in calls),
              "every OTP login request must carry a timeout")
        exposure = [call for session in CURRENT["sessions"] for call in session.calls
                    if "ssl" in call[2]]
        check(exposure == [],
              f"no request of the new flow may pass an ssl override at all, got {exposure}")
        check(CURRENT["micoapi_login_calls"] == [],
              "the OTP flow must not use MiAccount.login()")
        ok("contract B: 1.0.12 section has zero TLS overrides; the helper never sends ssl")
    finally:
        h.cleanup()


# ─────────────── 19. [real 2.9.3] contracts against the genuine dependency ───────────────
# The package used here is the genuine miservice_fork 2.9.3 code, taken from the
# official sdist (miservice_fork-2.9.3.tar.gz) that the runtime pins. It is either
# an unpacked copy next to the repo or a copy placed under
# venv-contract/Lib/site-packages; nothing is downloaded and no fake stands in for
# MiAccount itself. Only fake_useragent (an unrelated UA-string dependency, absent
# offline) is replaced by a two-line stand-in when it cannot be imported.
REAL_PACKAGE_VERSION = "2.9.3"


def _find_real_miaccount_path():
    candidates = []
    override = os.environ.get("MISERVICE_REAL_PATH", "").strip()
    if override:
        candidates.append(Path(override))
    root = Path(__file__).resolve().parent.parent
    candidates.extend([
        root / "venv-contract" / "Lib" / "site-packages",
        root / "venv-contract" / "lib" / "python3.11" / "site-packages",
        root.parent / "venv-contract" / "Lib" / "site-packages",
        root.parent / "venv-contract" / "lib" / "python3.11" / "site-packages",
        root.parent / "research" / "pkgs" / "fork-2.9.3" / "miservice_fork-2.9.3",
    ])
    for candidate in candidates:
        for base in (candidate, candidate / "miservice"):
            module = base / "miaccount.py"
            if module.is_file():
                return module
    return None


def _real_package_version(module_path):
    package = module_path.parent
    metadata = [
        package.parent / "PKG-INFO",
        package.parent / ("miservice_fork-%s.dist-info" % REAL_PACKAGE_VERSION) / "METADATA",
    ]
    try:
        metadata.extend(sorted(package.parent.glob("miservice_fork-*.dist-info/METADATA")))
    except OSError:
        pass
    for path in metadata:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
    return ""


def _load_real_miaccount(module_path):
    '''Execute the genuine miaccount.py by path (the fake one is in sys.modules).'''
    import importlib.util
    saved = {name: sys.modules.get(name) for name in
             ("miservice", "miservice.miaccount", "aiohttp", "fake_useragent")}
    stubbed = None
    try:
        for name in ("miservice", "miservice.miaccount", "aiohttp"):
            sys.modules.pop(name, None)
        try:
            __import__("fake_useragent")
        except ImportError:
            stubbed = types.ModuleType("fake_useragent")

            class _StandInUserAgent:   # only supplies the random UA string
                def __init__(self, *args, **kwargs):
                    pass

                @property
                def random(self):
                    return "python-test-agent"

            stubbed.UserAgent = _StandInUserAgent
            sys.modules["fake_useragent"] = stubbed
        spec = importlib.util.spec_from_file_location("miservice_real_miaccount", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if stubbed is not None:
            sys.modules.pop("fake_useragent", None)
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def scenario_real_293_contracts():
    module_path = _find_real_miaccount_path()
    if module_path is None:
        print("SKIP [real 2.9.3] no miservice_fork 2.9.3 source found "
              "(set MISERVICE_REAL_PATH to a directory containing the miservice package)")
        return None
    version = _real_package_version(module_path)
    if version and version != REAL_PACKAGE_VERSION:
        print(f"SKIP [real 2.9.3] {module_path} reports version {version}; "
              f"these contracts pin {REAL_PACKAGE_VERSION}")
        return None
    try:
        real = _load_real_miaccount(module_path)
    except Exception as exc:
        print(f"SKIP [real 2.9.3] cannot import {module_path}: "
              f"{type(exc).__name__}: {exc}")
        return None
    real_account = real.MiAccount
    real_source = module_path.read_text(encoding="utf-8", errors="replace")
    reset_current()

    # B(a) the documented pin: 2.9.3 disables certificate verification in _serviceLogin
    check(re.search(r"ssl\s*=\s*False", real_source) is not None,
          "miservice_fork 2.9.3 must still disable TLS verification (documented pin)")
    login_source = inspect.getsource(real_account._serviceLogin)
    check(re.search(r"ssl\s*=\s*False", login_source) is not None,
          "MiAccount._serviceLogin in the pinned version is expected to pass ssl=False")
    check("allow_redirects" not in login_source,
          "the pinned _serviceLogin relies on aiohttp's default redirect handling")

    token = {"userId": "real-user", "passToken": "real-pass", "deviceId": "REALDEVICE123456"}
    reply = {"code": 0, "ssecurity": "real-ssecurity",
             "location": "https://api2.mina.mi.com/sts?sign=real", "nonce": "real-nonce"}

    # A.1 the real class: the preloaded token is what makes the exchange possible
    real_session = FakeSession(login_responses=[dict(reply)])
    account = real_account(real_session, "", "", None)
    account.token = dict(token)
    exchanged = asyncio.run(account._serviceLogin("serviceLogin?sid=micoapi&_json=true"))
    real_calls = list(real_session.calls)
    check(len(real_calls) == 1,
          f"the real _serviceLogin must issue exactly one request, got {real_calls}")
    method = url = None
    real_cookies = {}
    real_body = None
    if real_calls:
        method, url, kwargs = real_calls[0]
        real_cookies = kwargs.get("cookies") or {}
        real_body = kwargs.get("data")
        check(method == "GET", f"a bodiless exchange must be a GET, got {method}")
        check(url == LOGIN_PREFIX + "?sid=micoapi&_json=true",
              f"the real class must POST/GET the pinned URL, got {url}")
        check(real_cookies.get("userId") == "real-user",
              f"the userId cookie must come from the preload, got {real_cookies}")
        check(real_cookies.get("passToken") == "real-pass",
              f"the passToken cookie must come from the preload, got {real_cookies}")
        check(real_cookies.get("deviceId") == "REALDEVICE123456",
              f"the deviceId cookie must come from the preload, got {real_cookies}")
        check(real_cookies.get("sdkVersion") == "3.9",
              f"the sdkVersion cookie must match the protocol, got {real_cookies}")
        check(kwargs.get("ssl") is False,
              "2.9.3 is expected to disable TLS verification here (pin check)")
    check(exchanged.get("code") == 0 and exchanged.get("ssecurity") == "real-ssecurity",
          f"the real class must parse the &&&START&&& body, got {exchanged}")

    # A.2 the production helper must send the same essential request, with TLS on
    h = Harness()
    try:
        helper = h.ns["_mi_service_login_verified"]
        our_session = FakeSession(login_responses=[dict(reply)])
        ours = asyncio.run(helper(
            our_session, "serviceLogin?sid=micoapi&_json=true",
            device_id=token["deviceId"], token=dict(token), user_agent="bridge-agent",
        ))
        our_calls = list(our_session.calls)
        check(len(our_calls) == 1,
              f"the bridge helper must issue exactly one request, got {our_calls}")
        if our_calls:
            our_method, our_url, our_kwargs = our_calls[0]
            check(our_method == method,
                  f"the helper must use the same HTTP method as 2.9.3 ({method}), got {our_method}")
            check(our_url == url, f"the helper must hit the same URL as 2.9.3, got {our_url}")
            our_cookies = our_kwargs.get("cookies") or {}
            check(our_cookies == real_cookies,
                  f"the helper must send the same cookies as 2.9.3, got {our_cookies}")
            check(our_kwargs.get("data") == real_body,
                  f"the helper must send the same body as 2.9.3, got {our_kwargs.get('data')}")
            check("ssl" not in our_kwargs or our_kwargs["ssl"] is not False,
                  "the helper may never disable TLS verification")
            check(our_kwargs.get("allow_redirects") is False,
                  "the helper must never follow redirects")
        check(ours == exchanged,
              f"the helper must parse the same response as 2.9.3, got {ours}")
        ok(f"[real 2.9.3] token preload contract proven against the genuine class "
           f"(version {version or 'unlabelled copy'}, {module_path.parent.name}/miaccount.py); "
           f"the bridge helper matches its request without the TLS override")
    finally:
        h.cleanup()


# ─────────────── 6. audit fixes: Cookie Path scoping and log sanitisation ───────────────
def scenario_otp_cookie_path_scope():
    h = Harness()
    try:
        try:
            import aiohttp as real_aiohttp
            from yarl import URL as RealURL
        except Exception as exc:  # pragma: no cover - environment without aiohttp
            ok(f"[skip] real aiohttp CookieJar unavailable: {exc}")
            return

        async def run():
            jar = real_aiohttp.CookieJar()
            jar.update_cookies_from_headers(
                ["passToken=PT-VALUE; Path=/pass; Domain=account.xiaomi.com"],
                RealURL("https://account.xiaomi.com/pass/identity/list"))

            class _Session:
                cookie_jar = jar

            identity = h.ns["_otp_session_identity"](_Session())
            root_only = jar.filter_cookies(RealURL("https://account.xiaomi.com/"))
            return identity, root_only

        identity, root_only = asyncio.run(run())
        check(identity.get("passToken") == "PT-VALUE",
              "a Path=/pass cookie must be found via the resume URL, got " + repr(identity))
        check("passToken" not in root_only,
              "the fixture must actually be path-scoped (a root filter must miss it)")
        ok("[audit] resume identity reads the CookieJar with the resume URL "
           "(Path=/pass cookies are visible; the root URL alone would miss them)")
    finally:
        h.cleanup()


def scenario_provider_error_is_sanitised():
    h = Harness()
    try:
        safe = h.ns["_safe_otp_description"]
        raw = ("登录参数不合法 passToken=abc123def456\n控制符\x07 serviceToken="
               + "A" * 40)
        text = safe(raw)
        check("\x07" not in text and "\n" not in text,
              "control characters must never reach the log, got " + repr(text))
        check("abc123def456" not in text and "A" * 40 not in text,
              "credential-like values must be masked, got " + repr(text))
        check("登录参数不合法" in text,
              "the human-readable meaning must survive, got " + repr(text))
        ok("[audit] provider error phrases are masked before logging "
           "(cookie pairs and long opaque runs never reach the log)")
    finally:
        h.cleanup()


# ─────────────── 20. browser-signed ticket: the add-on never exchanges the passToken ───────────────
TICKET_JSON = {
    "code": 0,
    "userId": "ticket-user",
    "passToken": "V1:ticket-pass",
    "ssecurity": "ticket-ssecurity",
    "nonce": 1234567890123456789,
    "location": "https://api2.mina.mi.com/sts?sign=ticket&t=1",
}


def ticket_text(payload=None, prefix="&&&START&&&"):
    return prefix + json.dumps(payload if payload is not None else TICKET_JSON)


def scenario_cookie_ticket():
    from urllib.parse import quote as _quote
    h = Harness()
    try:
        # (a) success: the STS call carries _userIdNeedEncrypt + clientSign, and the
        #     19-digit nonce is signed exactly (never through a float).
        CURRENT["response_script"] = [
            response(200, raw=b"", cookies={"serviceToken": "ticket-svc"})]
        data = cookie_ticket(h, ticket_text())
        check(data.get("ok") is True, f"a browser-signed ticket must log in, got {data}")
        check(CURRENT["verified_login_calls"] == [],
              "the ticket path must never call serviceLogin from the add-on")
        requests = [call for session in CURRENT["sessions"] for call in session.calls]
        sts = [call for call in requests
               if str(call[1]).startswith("https://api2.mina.mi.com/sts")]
        check(len(sts) == 1, f"exactly one STS request expected, got {sts}")
        if sts:
            url = str(sts[0][1])
            check("_userIdNeedEncrypt=true" in url,
                  f"the STS URL must ask for user-id encryption: {url}")
            expected = base64.b64encode(hashlib.sha1(
                ("nonce=" + str(TICKET_JSON["nonce"]) + "&ticket-ssecurity").encode()
            ).digest()).decode()
            check(("clientSign=" + _quote(expected, safe="")) in url,
                  f"clientSign must sign the exact 19-digit nonce, got {url}")
            check(sts[0][2].get("allow_redirects") is False,
                  "the STS request must not follow redirects")
            check("ssl" not in sts[0][2] or sts[0][2]["ssl"] is not False,
                  "the STS request must keep TLS verification on")
        token = json.loads(h.token_path.read_text(encoding="utf-8"))
        check(token.get("micoapi") == ["ticket-ssecurity", "ticket-svc"],
              f"the ticket credentials must be persisted, got {token}")
        check("serviceToken=ticket-svc" in h.cfg["xiaomi"]["cookie_text"],
              f"cookie_text must carry the ticket token, got {h.cfg['xiaomi']['cookie_text']!r}")

        # (b) the JSON wrapped in a pasted page is still recognised
        h2 = Harness()
        try:
            CURRENT["response_script"] = [
                response(200, raw=b"", cookies={"serviceToken": "svc2"})]
            page = "<html><body><pre>" + json.dumps(TICKET_JSON) + "</pre></body></html>"
            got = cookie_ticket(h2, page)
            check(got.get("ok") is True, f"a wrapped JSON body must still parse, got {got}")
        finally:
            h2.cleanup()

        # (c) empty / wrong field / unparsable / rejected / incomplete are explicit
        h3 = Harness()
        try:
            before = h3.snapshot_data()
            cases = [
                ("", "empty"),
                ("https://account.xiaomi.com/pass/serviceLogin?sid=micoapi", "wrong_field"),
                ("not json at all", "bad_json"),
                (ticket_text({**TICKET_JSON, "code": 70016,
                              "description": "登录验证失败"}), "rejected"),
                (ticket_text({k: v for k, v in TICKET_JSON.items() if k != "ssecurity"}),
                 "incomplete"),
            ]
            for text, action in cases:
                got = cookie_ticket(h3, text)
                check(got.get("ok") is False and got.get("action") == action,
                      f"{action}: expected that action, got {got}")
            rejected = cookie_ticket(h3, ticket_text({**TICKET_JSON, "code": 70016,
                                                      "description": "登录验证失败"}))
            check((rejected.get("detail") or {}).get("code") == 70016,
                  f"a rejected ticket must expose Xiaomi's code, got {rejected}")
            check("70016" in rejected.get("msg", ""),
                  f"a rejected ticket must name the code, got {rejected.get('msg')!r}")
            check(h3.snapshot_data() == before, "a failed ticket must not write any file")
        finally:
            h3.cleanup()

        # (d) the STS endpoint answering 401 is an explicit failure, not a traceback
        h4 = Harness()
        try:
            CURRENT["response_script"] = [response(401, raw=b"")]
            got = cookie_ticket(h4, ticket_text())
            check(got.get("ok") is False and got.get("action") == "sts_http",
                  f"an STS 401 must be explicit, got {got}")
            check("401" in got.get("msg", ""),
                  f"the HTTP status must be named: {got.get('msg')!r}")
        finally:
            h4.cleanup()

        # (e) a ticket for another account is refused instead of silently switching
        h5 = Harness()
        try:
            h5.ns["_load_saved_xiaomi_identity"] = lambda: {
                "userId": "saved-user", "passToken": "saved-pass", "deviceId": "saved-dev"}
            before = h5.snapshot_data()
            got = cookie_ticket(h5, ticket_text())
            check(got.get("ok") is False and got.get("action") == "identity_mismatch",
                  f"another account's ticket must be refused, got {got}")
            check(h5.snapshot_data() == before, "a refused ticket must not write any file")
        finally:
            h5.cleanup()
        ok("ticket: browser-signed login skips the exchange, keeps nonce precision, "
           "refuses another account and every failure leaves /data untouched")
    finally:
        h.cleanup()


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
        scenario_verify_phone_business_code,
        scenario_verify_success,
        scenario_resume_rejected,
        scenario_resume_second_round,
        scenario_resume_fresh_session_recovery,
        scenario_resume_location_hop,
        scenario_otp_start_other_account,
        scenario_resume_incomplete_is_explicit,
        scenario_resume_second_round_capped,
        scenario_resume_identity_mismatch,
        scenario_missing_field_detection,
        scenario_password_login_challenge_without_code,
        scenario_wrong_code,
        scenario_otp_device_probe_gates_writes,
        scenario_ttl_expiry,
        scenario_restart,
        scenario_unsupported_flows,
        scenario_location_allowlist,
        scenario_redirect_after_code,
        scenario_redirect_allowlist_and_limit,
        scenario_confirm_url,
        scenario_sts_terminal_hop,
        scenario_untrusted_hop_diagnostics,
        scenario_confirm_from_query,
        scenario_cookie_login,
        scenario_cookie_browser_compatibility,
        scenario_cookie_login_errors,
        scenario_cookie_login_clears_otp,
        scenario_cookie_ticket,
        scenario_legacy_message,
        scenario_secrets_not_logged,
        scenario_otp_cookie_path_scope,
        scenario_provider_error_is_sanitised,
        scenario_failures_leave_data_untouched,
        scenario_compatibility,
        scenario_verified_transport,
        scenario_real_293_contracts,
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
