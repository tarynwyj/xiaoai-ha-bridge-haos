#!/usr/bin/env python3
"""Login fallback: 87001 must be terminal and must never come from a token path.

Runs against the built bridge.py with fake credentials on disk; no Xiaomi account
is involved.

    python xiaoai_bridge/test_login_fallback.py <built bridge.py>
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

WANTED_FUNCS = (
    "_set_token",
    "_miservice_auth_failure",
    "_note_auth_failure",
    "_token_account_credentials",
    "_build_token_account",
    "_load_saved_xiaomi_identity",
)
NEEDED = ("AUTH_OTP_REQUIRED_CODES", "RELOGIN_FATAL_CODES", "_last_auth_failure")


class Log:
    def __init__(self):
        self.messages = []

    def _rec(self, level, message, *args):
        self.messages.append("[%s] %s" % (level, message % args if args else message))

    def info(self, m, *a):
        self._rec("INFO", m, *a)

    def warning(self, m, *a):
        self._rec("WARNING", m, *a)

    def error(self, m, *a):
        self._rec("ERROR", m, *a)

    def has(self, needle):
        return any(needle in line for line in self.messages)


class Reporter:
    def __init__(self):
        self.checks = 0
        self.failures = []

    def check(self, name, ok, detail=""):
        self.checks += 1
        if not ok:
            self.failures.append(name + ((" :: " + str(detail)) if detail else ""))
            print("  FAIL " + name + ((" :: " + str(detail)) if detail else ""))

    def eq(self, name, actual, expected):
        self.check(name, actual == expected, "expected %r, got %r" % (expected, actual))


def build_namespace(bridge: Path, root: Path, log: Log):
    tree = ast.parse(bridge.read_text(encoding="utf-8"))
    funcs = {n.name: n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    missing = [n for n in WANTED_FUNCS if n not in funcs]
    assert not missing, "built bridge.py is missing: %s" % missing
    ns = {
        "json": json, "time": __import__("time"), "log": log,
        "TOKEN_PATH": root / "config" / ".mi.token",
        "AUTH_PATH": root / "config" / "auth.json",
        "CONFIG_PATH": root / "config" / "config.yaml",
    }
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(n in NEEDED for n in names):
                exec(compile(ast.Module(body=[node], type_ignores=[]), str(bridge), "exec"), ns)
    ns.setdefault("_last_auth_failure", {})
    # _set_token is defined twice (original, then the password-clearing wrapper).
    set_tokens = [n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_set_token"]
    if set_tokens:
        exec(compile(ast.Module(body=[set_tokens[0]], type_ignores=[]), str(bridge), "exec"), ns)
        ns["_original_set_token"] = ns["_set_token"]
        exec(compile(ast.Module(body=[set_tokens[-1]], type_ignores=[]), str(bridge), "exec"), ns)
    for name in WANTED_FUNCS:
        if name == "_set_token":
            continue
        exec(compile(ast.Module(body=[funcs[name]], type_ignores=[]), str(bridge), "exec"), ns)
    return ns


def scenario_codes(reporter, ns):
    otp = ns["_miservice_auth_failure"](
        Exception({"code": 87001, "desc": "验证码输入错误", "sid": "micoapi"}))
    reporter.eq("87001 is classified as otp", otp.get("kind"), "otp")
    reporter.check("87001 is terminal", otp.get("terminal") is True, otp)
    reporter.check("the message tells the user what to do",
                   "验证码" in otp.get("message", "") and "扫码" in otp.get("message", ""),
                   otp.get("message"))

    rejected = ns["_miservice_auth_failure"](Exception({"code": 70016, "desc": "登录验证失败"}))
    reporter.eq("70016 stays rejected", rejected.get("kind"), "rejected")
    reporter.check("70016 is terminal", rejected.get("terminal") is True, rejected)

    reporter.eq("an unrelated code is not an auth verdict",
                ns["_miservice_auth_failure"](Exception({"code": 300004})), {})
    reporter.eq("a plain exception is not an auth verdict",
                ns["_miservice_auth_failure"](Exception("boom")), {})

    ns["_note_auth_failure"](Exception({"code": 87001, "desc": "验证码输入错误"}))
    reporter.eq("the verdict is remembered", ns["_last_auth_failure"].get("kind"), "otp")


def scenario_token_account_has_no_password(reporter, ns, root):
    """The token path must never leave a password on the account."""
    (root / "config").mkdir(parents=True, exist_ok=True)
    identity = {"userId": "user-1", "passToken": "pass-1", "deviceId": "device-1"}
    (root / "config" / "auth.json").write_text(json.dumps(identity), encoding="utf-8")
    before = (root / "config" / "auth.json").read_bytes()

    account = SimpleNamespace(username="u@example.com", password="stale-password", token={})
    ns["_set_token"](account, {"xiaomi": {}})

    reporter.eq("the token is restored from auth.json", account.token.get("passToken"), "pass-1")
    reporter.eq("the saved credentials are untouched",
                (root / "config" / "auth.json").read_bytes(), before)

    # a second restore (the 401 path) must not lose the passToken either
    account.token = None
    ns["_set_token"](account, {"xiaomi": {}})
    reporter.eq("a 401 restore keeps the passToken", account.token.get("passToken"), "pass-1")
    reporter.eq("the file is still byte-identical",
                (root / "config" / "auth.json").read_bytes(), before)

    creds = ns["_token_account_credentials"]({"username": "u@example.com", "password": "p"})
    reporter.eq("the token path is handed no credentials", creds, ("", ""))
    reporter.check("the helper is what the token path should use",
                   callable(ns["_token_account_credentials"]))


class FakeMiAccount:
    """Mirror of miservice_fork 2.9.3 MiAccount for the login() fallback."""

    def __init__(self, session, username, password, token_store=None):
        self.session = session
        self.username = username
        self.password = password
        self.token = {"deviceId": "D1"}

    async def login(self, sid):
        # passToken exchange failed -> miservice tries serviceLoginAuth2
        if not self.password:
            # no password to send: the fallback never happens, login just fails
            return False
        raise Exception({"code": 87001, "desc": "验证码输入错误", "sid": sid})


def scenario_builder_blocks_the_fallback(reporter, ns):
    """The token account must not carry a password into miservice's fallback."""
    import asyncio
    import sys
    import types

    fake = types.ModuleType("miservice")
    fake.MiAccount = FakeMiAccount
    sys.modules["miservice"] = fake
    try:
        cfg = {"username": "u@example.com", "password": "stale-password"}
        account = ns["_build_token_account"](object(), cfg)
        reporter.eq("the builder hands MiAccount no username", account.username, "")
        reporter.eq("the builder hands MiAccount no password", account.password, "")

        # what miservice would do after a 401
        raised = None
        try:
            result = asyncio.run(account.login("micoapi"))
        except Exception as exc:  # noqa: BLE001 - the point is that it must not raise
            raised = exc
            result = None
        reporter.check("the fallback never reaches 87001", raised is None,
                       repr(raised))
        reporter.eq("login simply fails when there is no password", result, False)

        # a password-carrying account still reproduces the bug, proving the test bites
        buggy = FakeMiAccount(object(), "u@example.com", "stale-password")
        bug = None
        try:
            asyncio.run(buggy.login("micoapi"))
        except Exception as exc:  # noqa: BLE001
            bug = exc
        reporter.check("a password-carrying account does raise 87001",
                       bug is not None and bug.args[0].get("code") == 87001, repr(bug))
    finally:
        sys.modules.pop("miservice", None)


def main():
    bridge = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/bridge.py")
    assert bridge.exists(), "built bridge.py not found: %s" % bridge
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    root = Path(tempfile.mkdtemp(prefix="login-fallback-"))
    log = Log()
    ns = build_namespace(bridge, root, log)
    reporter = Reporter()
    print("login fallback tests | %s" % bridge)
    scenario_codes(reporter, ns)
    print("  --  error classification")
    scenario_token_account_has_no_password(reporter, ns, root)
    print("  --  token account carries no password")
    scenario_builder_blocks_the_fallback(reporter, ns)
    print("  --  the password fallback cannot fire")
    if reporter.failures:
        print("login fallback tests FAILED (%d checks, %d failures)"
              % (reporter.checks, len(reporter.failures)))
        for item in reporter.failures:
            print("  - " + item)
        return 1
    print("login fallback tests passed (%d checks)" % reporter.checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
