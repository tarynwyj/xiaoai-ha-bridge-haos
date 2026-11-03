"""Regression tests for the Xiaomi conversation-polling authentication.

Context (upstream issue, also reported as PR #4 by @caideone): the bridge injected
the account cookies into the shared ``aiohttp`` CookieJar and then polled
``userprofile.mina.mi.com`` with only the speaker ``deviceId`` in the request-local
cookie mapping. aiohttp keeps the domain on the cookie morsels, so injecting one
``SimpleCookie`` for several hosts stores every cookie under the first host and the
polling host receives **no** ``userId``/``serviceToken``: device discovery and Home
Assistant work, but no voice command is ever seen (UI "connected", command count 0).

These tests run against the *patched* ``bridge.py`` and pin down:
  1. every poll request carries the Xiaomi auth cookies (not just ``deviceId``);
  2. the selected speaker ``deviceId`` wins over the stored one;
  3. the cookies only ever go to the conversation host, and a redirect is not
     followed (aiohttp re-sends explicit ``cookies=`` to the redirect target);
  4. a 401 still asks for a re-login, a redirect does not;
  5. the CookieJar injection now really reaches the poll host;
  6. installs with only ``cookie_text`` (no .mi.token) are covered.

Usage: python test_poll_auth.py [/app/bridge.py]
"""

import ast
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import aiohttp

source_path = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/bridge.py")
source = source_path.read_text(encoding="utf-8")
tree = ast.parse(source)
built = source

POLL_HOST = "userprofile.mina.mi.com"
checks = 0
failures = []


def check(condition, message):
    global checks
    checks += 1
    if not condition:
        failures.append(message)


class Logger:
    def __init__(self):
        self.messages = []

    def _add(self, level, message, args):
        self.messages.append(f"[{level}] {message % args if args else message}")

    def debug(self, message, *args):
        self._add("DEBUG", message, args)

    info = warning = error = debug

    def has(self, needle):
        return any(needle in message for message in self.messages)


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status = status
        self._payload = payload
        self.headers = headers or {}
        self.body_read = False

    async def json(self):
        self.body_read = True
        return self._payload


class FakeSession:
    """Records every request; returns scripted responses."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse(200, {"data": None})


def build_namespace(root: Path, cfg: dict, log: Logger):
    token_path = root / "config" / ".mi.token"
    auth_path = root / "config" / "auth.json"
    names = (
        "_valid_saved_cookie_text",
        "_get_cookie",
        "_poll_auth_cookies",
        "_apply_cookie_to_session",
        "_get_latest_ask_from_xiaoai",
    )
    namespace = {
        "TOKEN_PATH": token_path,
        "AUTH_PATH": auth_path,
        "COOKIE_TEMPLATE": "deviceId={device_id}; serviceToken={service_token}; userId={user_id}",
        "LATEST_ASK_API": (
            "https://userprofile.mina.mi.com/device_profile/v2/conversation"
            "?source=dialogu&hardware={hardware}&timestamp={timestamp}&limit=2"
        ),
        "json": json,
        "log": log,
        "load_config": lambda: cfg,
        "aiohttp": SimpleNamespace(
            ClientTimeout=lambda **kwargs: ("timeout", kwargs),
            ClientSession=object,
        ),
    }
    # Module level constants the extracted functions rely on (e.g. the cookie
    # allow-list). Explicit values above win.
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name.isupper() and name not in namespace:
                try:
                    namespace[name] = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    pass

    for name in names:
        node = next(
            (node for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name),
            None,
        )
        if node is None:
            failures.append(f"patched bridge.py is missing {name}()")
            continue
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace


def make_data(root: Path, *, micoapi=("ssecurity-1", "service-token-1"),
              user_id="user-1", pass_token="pass-1", stored_device="stored-device",
              cookie_text="", write_token=True):
    (root / "config").mkdir(parents=True, exist_ok=True)
    token_path = root / "config" / ".mi.token"
    auth_path = root / "config" / "auth.json"
    if write_token and micoapi is not None:
        data = {"userId": user_id, "passToken": pass_token, "deviceId": stored_device}
        if micoapi:
            data["micoapi"] = list(micoapi)
        token_path.write_text(json.dumps(data), encoding="utf-8")
        auth_path.write_text(
            json.dumps({"userId": user_id, "passToken": pass_token, "deviceId": stored_device}),
            encoding="utf-8",
        )
    cfg = {
        "xiaomi": {
            "username": "",
            "password": "",
            "cookie_text": cookie_text,
            "device_did": stored_device,
            "hardware": "L05C",
            "tts_reply": True,
        },
        "homeassistant": {"url": "http://homeassistant:80", "token": "ha-secret"},
        "intent_rules": [],
        "poll_interval_seconds": 2,
    }
    return cfg


def run_poll(namespace, session, device_id="speaker-target"):
    return asyncio.run(
        namespace["_get_latest_ask_from_xiaoai"](session, device_id, "L05C", 1_700_000_000_000)
    )


def scenario_auth_cookies_present(root):
    cfg = make_data(root)
    log = Logger()
    namespace = build_namespace(root, cfg, log)
    session = FakeSession()
    records, _, need_relogin = run_poll(namespace, session)

    check(len(session.calls) == 1, f"expected exactly one poll request, got {len(session.calls)}")
    url, kwargs = session.calls[0]
    cookies = kwargs.get("cookies") or {}
    # The bug: only {"deviceId": ...} used to be sent, so Xiaomi answered 401/empty.
    check(cookies.get("userId") == "user-1", f"poll must carry userId, got {sorted(cookies)}")
    check(cookies.get("serviceToken") == "service-token-1",
          f"poll must carry serviceToken, got {sorted(cookies)}")
    check(set(cookies) == {"userId", "serviceToken", "deviceId"},
          f"poll must not carry anything besides the three needed cookies, got {sorted(cookies)}")
    check(cookies.get("deviceId") == "speaker-target",
          f"poll must keep the selected speaker deviceId, got {cookies.get('deviceId')!r}")
    check(urlsplit(url).hostname == POLL_HOST, f"poll must target {POLL_HOST}, got {url}")
    check(kwargs.get("allow_redirects") is False,
          "poll must not follow redirects while carrying auth cookies")
    check(need_relogin is False and records == [], "no records must not request a re-login")
    return "poll request carries userId + serviceToken + selected deviceId"


def scenario_target_device_wins(root):
    # The stored cookie_text names a different speaker than the one being polled.
    cfg = make_data(
        root,
        cookie_text="userId=user-1; passToken=pass-1; serviceToken=cookie-svc; deviceId=stored-device",
    )
    log = Logger()
    namespace = build_namespace(root, cfg, log)
    session = FakeSession()
    run_poll(namespace, session, device_id="study-lamp")
    cookies = session.calls[0][1]["cookies"]
    check(cookies["deviceId"] == "study-lamp",
          f"selected speaker must win, got {cookies['deviceId']!r}")
    return "target speaker deviceId overrides the stored one"


def scenario_cookie_only_does_not_send_pass_token(root):
    """A pasted browser cookie contains passToken; the poll must not forward it."""
    cfg = make_data(
        root,
        write_token=False,
        cookie_text=(
            "userId=qr-user; passToken=qr-pass; serviceToken=qr-svc; deviceId=qr-dev; "
            "cUserId=qr-cuid; refreshToken=qr-refresh"
        ),
    )
    log = Logger()
    namespace = build_namespace(root, cfg, log)
    session = FakeSession()
    run_poll(namespace, session)
    cookies = session.calls[0][1]["cookies"]
    check(cookies.get("userId") == "qr-user" and cookies.get("serviceToken") == "qr-svc",
          f"cookie-only install must still send auth, got {sorted(cookies)}")
    check(set(cookies) == {"userId", "serviceToken", "deviceId"},
          f"poll must send exactly userId+serviceToken+deviceId, got {sorted(cookies)}")
    check("passToken" not in cookies,
          f"passToken is a login credential and must never reach the poll, got {sorted(cookies)}")
    check("refreshToken" not in cookies and "cUserId" not in cookies,
          f"unknown cookie fields must not be forwarded, got {sorted(cookies)}")
    return "cookie-only poll sends userId+serviceToken+deviceId and never passToken"


def scenario_incomplete_micoapi_falls_back_to_complete_cookie(root):
    cfg = make_data(
        root, micoapi=(),
        cookie_text="userId=fallback-user; passToken=login-only; serviceToken=fallback-svc; deviceId=d",
    )
    log = Logger()
    namespace = build_namespace(root, cfg, log)
    saved = namespace["_get_cookie"](cfg)
    check(saved is not None and saved.get("serviceToken") == "fallback-svc",
          f"incomplete .mi.token must fall back to a complete cookie_text: {saved}")
    session = FakeSession()
    run_poll(namespace, session)
    cookies = session.calls[0][1]["cookies"]
    check(cookies.get("userId") == "fallback-user"
          and cookies.get("serviceToken") == "fallback-svc",
          f"poll must use the complete fallback credentials, got {cookies}")
    return "missing micoapi serviceToken falls back to a complete saved Cookie"


def scenario_incomplete_cookie_never_masquerades_as_poll_auth(root):
    cfg = make_data(root, write_token=False, cookie_text="userId=u; passToken=p")
    log = Logger()
    namespace = build_namespace(root, cfg, log)
    check(namespace["_get_cookie"](cfg) is None,
          "passToken-only cookie lacks the serviceToken required for polling")
    return "incomplete Cookie text is not treated as a valid poll authentication"


def scenario_no_credentials(root):
    cfg = make_data(root, write_token=False, cookie_text="")
    log = Logger()
    namespace = build_namespace(root, cfg, log)
    session = FakeSession([FakeResponse(401)])
    records, _, need_relogin = run_poll(namespace, session)
    check(need_relogin is True, "a 401 must still request a re-login")
    cookies = session.calls[0][1]["cookies"]
    check(cookies == {"deviceId": "speaker-target"},
          f"without credentials only deviceId may be sent, got {cookies}")
    return "no credentials -> only deviceId, and 401 still triggers re-login"


def scenario_redirect_does_not_leak(root):
    cfg = make_data(root)
    log = Logger()
    namespace = build_namespace(root, cfg, log)
    leak_target = "https://attacker.example/steal"
    session = FakeSession([FakeResponse(302, headers={"Location": leak_target})])
    records, _, need_relogin = run_poll(namespace, session)

    check(len(session.calls) == 1,
          f"a redirect must not be followed, saw {len(session.calls)} request(s)")
    hosts = {urlsplit(url).hostname for url, _ in session.calls}
    check(hosts == {POLL_HOST}, f"auth cookies must only reach {POLL_HOST}, saw {hosts}")
    check(leak_target not in [url for url, _ in session.calls],
          "the redirect target must never be requested with the auth cookies")
    check(records == [] and need_relogin is False,
          "a redirect is not an auth failure: no records, no re-login")
    check(log.has("已停止跟随"), "a redirected poll must be logged")
    return "a 302 is not followed, so cookies never reach another host"


def scenario_jar_least_privilege(root):
    """The CookieJar is only used by the polling hosts and only for session cookies."""
    cfg = make_data(
        root,
        write_token=False,
        cookie_text="userId=qr-user; passToken=qr-pass; serviceToken=qr-svc; deviceId=qr-dev",
    )
    log = Logger()
    namespace = build_namespace(root, cfg, log)
    session = FakeSession()

    async def exercise():
        import yarl

        jar = aiohttp.CookieJar()
        session.cookie_jar = jar
        applied = namespace["_apply_cookie_to_session"](session, cfg)

        def cookies_for(url):
            return {k: m.value for k, m in jar.filter_cookies(yarl.URL(url)).items()}

        return (
            applied,
            cookies_for(
                "https://userprofile.mina.mi.com/device_profile/v2/conversation?limit=2"
            ),
            cookies_for("https://api2.mina.mi.com/remote/ubus"),
            cookies_for("https://account.xiaomi.com/pass/serviceLoginAuth2"),
        )

    applied, poll_cookies, mina_cookies, account_cookies = asyncio.run(exercise())
    check(applied is True, "_apply_cookie_to_session must report success")
    check(poll_cookies.get("userId") == "qr-user"
          and poll_cookies.get("serviceToken") == "qr-svc",
          f"the CookieJar must serve auth cookies to the poll host, got {sorted(poll_cookies)}")
    check(set(poll_cookies) == {"userId", "serviceToken"},
          f"the poll host must only get the two session cookies, got {sorted(poll_cookies)}")
    check("passToken" not in poll_cookies and "passToken" not in mina_cookies,
          "passToken must not be injected into the mina domains")
    check(account_cookies == {},
          f"the login domain is filled by the login flow, not here: got {sorted(account_cookies)}")
    return "CookieJar gets userId+serviceToken for polling hosts only, never passToken"


def scenario_login_and_refresh_still_get_pass_token(root):
    """QR login / token refresh keep using passToken: only the poll paths filter it."""
    cfg = make_data(
        root,
        write_token=False,
        cookie_text="userId=qr-user; passToken=qr-pass; serviceToken=qr-svc; deviceId=qr-dev",
    )
    log = Logger()
    namespace = build_namespace(root, cfg, log)
    raw = namespace["_get_cookie"](cfg) or {}

    check(raw.get("passToken") == "qr-pass",
          "exactly: _get_cookie must stay unchanged for the login/refresh paths")
    check("POLL_COOKIE_KEYS" in built,
          "the poll allow-list must be an explicit constant in the patched bridge")
    check('saved["passToken"]' in built,
          "the refresh path must still read the saved passToken")
    check('"passToken": pass_token' in built,
          "the micoapi exchange must still send passToken to the login endpoint")
    check("passToken" in raw and "passToken" not in namespace["_poll_auth_cookies"]("dev"),
          "the filter must apply to the poll only")
    return "QR login and micoapi refresh keep passToken; only the poll filters it"


def scenario_redirect_would_leak_without_the_flag(root):
    """Negative control: the shipped ``allow_redirects=False`` is what prevents the leak."""
    try:

        async def leak_demo():
            from aiohttp import web

            async def handler(request):
                if request.path == "/start":
                    raise web.HTTPFound("/other")
                return web.Response(text=request.headers.get("Cookie", ""))

            app = web.Application()
            app.router.add_get("/start", handler)
            app.router.add_get("/other", handler)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"http://127.0.0.1:{port}/start",
                        cookies={"userId": "u", "serviceToken": "s"},
                    ) as resp:
                        return await resp.text()
            finally:
                await runner.cleanup()

        leaked = asyncio.run(leak_demo())
    except OSError as e:  # pragma: no cover - sandbox without a loopback listener
        print(f"SKIPPED redirect negative control: cannot bind loopback listener ({e})")
        return None
    check("serviceToken=s" in leaked and "userId=u" in leaked,
          "control failed: aiohttp should re-send explicit cookies across a redirect")
    return "negative control: without allow_redirects=False the cookies do leak"


def scenario_source_contract():
    check("cookies = _poll_auth_cookies(device_id)" in built,
          "the poll request must build its cookies through _poll_auth_cookies()")
    check('cookies = {"deviceId": device_id}' not in built,
          "the old deviceId-only cookie mapping must be gone")
    check("allow_redirects=False" in built, "the poll request must disable redirects")
    return "built bridge.py uses _poll_auth_cookies() and disables redirects"


def main():
    results = []
    scenarios = [
        scenario_auth_cookies_present,
        scenario_target_device_wins,
        scenario_cookie_only_does_not_send_pass_token,
        scenario_incomplete_micoapi_falls_back_to_complete_cookie,
        scenario_incomplete_cookie_never_masquerades_as_poll_auth,
        scenario_no_credentials,
        scenario_redirect_does_not_leak,
        scenario_jar_least_privilege,
        scenario_login_and_refresh_still_get_pass_token,
    ]
    for scenario in scenarios:
        with tempfile.TemporaryDirectory() as temp_dir:
            results.append(scenario(Path(temp_dir)))
    control = scenario_redirect_would_leak_without_the_flag(Path(tempfile.mkdtemp()))
    if control:
        results.append(control)
    results.append(scenario_source_contract())

    if failures:
        print(f"poll auth: {checks} checks, {len(failures)} FAILED")
        for failure in failures:
            print(f"  FAIL {failure}")
        sys.exit(1)

    for line in results:
        print(f"  ok  {line}")
    print(f"poll auth tests passed ({checks} checks, {len(results)} scenarios)")


if __name__ == "__main__":
    main()
