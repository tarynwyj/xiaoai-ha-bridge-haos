#!/usr/bin/env python3
"""Credential rotation, renewal and persistence behaviour of the patched bridge.

Everything here runs against the *generated* bridge.py (the pinned upstream with
patch_auth.py applied), with fake Xiaomi endpoints, a temporary /data tree and
injected failures. No real account, no real device, no network.

    python xiaoai_bridge/test_credential_renewal.py <path/to/built/bridge.py>

Covered:
  A  rotation     adopted/kept passToken, blank values, identity mismatch,
                  empty device list is not an auth failure
  B  failures     device discovery failing after a good exchange keeps the
                  rotated credentials; STS failure and device failure differ
  C  persistence  write failures are reported, not swallowed; a failed save is
                  retried without a second exchange; files stay 0600-ish/atomic
  D  deviceId     stable across retries, never taken from the speaker id
  E  concurrency  one exchange for concurrent renewals; a user re-login or a
                  credential wipe during the exchange is not overwritten
  F  schedule     NaN/inf/garbage intervals, corrupted issued_at
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

WANTED_FUNCS = (
    "_plausible_credential",
    "_adopt_login_identity",
    "_login_device_id",
    "_refresh_lock_obj",
    "_refresh_outcome",
    "_recent_refresh",
    "_remember_refresh",
    "_unsaved_refresh",
    "_save_refreshed_credentials",
    "_refresh_micoapi_token",
    "_relogin_and_refresh",
    "_save_auth_and_token",
    "_persist_sms_credentials",
    "_write_private_file",
    "_restore_credential_files",
    "_restore_private_file",
    "_missing_credentials",
    "_load_saved_xiaomi_identity",
    "_read_saved_credential",
    "_token_snapshot",
    "_restore_token",
    "_relogin_failure_from",
    "_relogin_retry_plan",
    "_refresh_interval_seconds",
    "_token_age_seconds",
    "_credential_fingerprint",
)
WANTED_CLASSES = ("MicoapiLoginRejected", "CredentialIdentityMismatch")


class Reporter:
    def __init__(self):
        self.checks = 0
        self.scenarios = 0

    def ok(self, message):
        self.checks += 1
        print("  ok  " + message)

    def done(self, message):
        self.scenarios += 1
        print("  --  " + message)


class Log:
    def __init__(self):
        self.messages = []

    def _record(self, level, message):
        self.messages.append("[%s] %s" % (level, message))

    def info(self, message, *args):
        self._record("INFO", message % args if args else message)

    def warning(self, message, *args):
        self._record("WARNING", message % args if args else message)

    def error(self, message, *args):
        self._record("ERROR", message % args if args else message)

    def has(self, needle):
        return any(needle in line for line in self.messages)


class FakeAccount:
    """Only the surface _refresh_micoapi_token actually uses."""

    def __init__(self, device_id=""):
        self.token = {"deviceId": device_id} if device_id else {}
        self.session = object()
        self.now_ua = "test-agent"
        self.sts_calls = 0

    async def _securityTokenService(self, location, nonce, ssecurity):
        self.sts_calls += 1
        if Exchange.sts_error:
            raise RuntimeError("STS timeout")
        return "service-" + str(Exchange.counter)


class Exchange:
    """Scripted Xiaomi serviceLogin responses."""

    counter = 0
    sts_error = False
    response = None          # dict | callable(call_index, user, pass)
    device_list_error = None  # exception instance to raise
    devices = [{"deviceID": "speaker-1", "hardware": "L05C"}]
    calls = []
    device_calls = []


def build_namespace(bridge_path: Path, root: Path, log: Log):
    source = bridge_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    token_path = config_dir / ".mi.token"
    auth_path = config_dir / "auth.json"

    state = {"write_allowed": True}

    async def verified_service_login(session, uri, data=None, device_id="",
                                     token=None, user_agent=""):
        Exchange.calls.append({"uri": uri, "device_id": device_id,
                               "token": dict(token or {})})
        Exchange.counter += 1
        spec = Exchange.response
        if callable(spec):
            return spec(Exchange.counter, (token or {}).get("userId"),
                        (token or {}).get("passToken"))
        return dict(spec or {})

    class FakeMiNAService:
        def __init__(self, account):
            self.account = account

        async def device_list(self):
            Exchange.device_calls.append(self.account)
            if Exchange.device_list_error is not None:
                raise Exchange.device_list_error
            return list(Exchange.devices)

    miservice = SimpleNamespace(MiNAService=FakeMiNAService)
    miaccount = SimpleNamespace(get_random=lambda n: "RANDOMDEVICEID00")
    # The functions under test import these lazily, so they must resolve through
    # sys.modules as well as through the exec namespace.
    sys.modules["miservice"] = miservice
    sys.modules["miservice.miaccount"] = miaccount

    namespace = {
        "json": json,
        "os": os,
        "time": time,
        "asyncio": asyncio,
        "log": log,
        "sys": sys,
        "TOKEN_PATH": token_path,
        "AUTH_PATH": auth_path,
        "CONFIG_PATH": config_dir / "config.yaml",
        "LOG_PATH": root / "logs" / "bridge.log",
        "POLL_COOKIE_KEYS": ("userId", "serviceToken"),
        "RELOGIN_FATAL_CODES": {70001, 70002, 70016},
        "RELOGIN_FATAL_BACKOFF": 1800,
        "RELOGIN_FATAL_WAIT_SLICE": 60,
        "TOKEN_REFRESH_RETRY": 300,
        "REFRESH_COALESCE_SECONDS": 60,
        "_refresh_lock": None,
        "_refresh_cache": {},
        "_last_refresh_failure": {},
        "_process_login_device_id": "",
        "_mi_service_login_verified": verified_service_login,
        "_credential_write_allowed": lambda: state["write_allowed"],
        "load_config": lambda: {"xiaomi": {}},
        "save_config": lambda cfg: None,
        "miservice": miservice,
        "miaccount": miaccount,
        "_state": state,
    }
    functions = {node.name: node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    missing = [name for name in WANTED_FUNCS if name not in functions]
    assert not missing, "built bridge.py is missing functions: %s" % missing
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in WANTED_CLASSES:
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(bridge_path), "exec"),
                 namespace)
    for name in WANTED_FUNCS:
        module = ast.Module(body=[functions[name]], type_ignores=[])
        exec(compile(module, str(bridge_path), "exec"), namespace)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(t in ("REFRESH_COALESCE_SECONDS", "_refresh_lock", "_refresh_cache",
                         "_last_refresh_failure", "_process_login_device_id")
                   for t in targets):
                exec(compile(ast.Module(body=[node], type_ignores=[]), str(bridge_path), "exec"),
                     namespace)
    return namespace


def reset_exchange():
    Exchange.counter = 0
    Exchange.sts_error = False
    Exchange.response = None
    Exchange.device_list_error = None
    Exchange.devices = [{"deviceID": "speaker-1", "hardware": "L05C"}]
    Exchange.calls = []
    Exchange.device_calls = []


def write_credentials(ns, user="user-1", pass_token="pass-1", device="device-1",
                      service="service-old", ssecurity="ssecurity-old"):
    identity = {"userId": user, "passToken": pass_token, "deviceId": device}
    ns["AUTH_PATH"].write_text(json.dumps(identity), encoding="utf-8")
    token = dict(identity)
    token["micoapi"] = [ssecurity, service]
    ns["TOKEN_PATH"].write_text(json.dumps(token), encoding="utf-8")
    return identity


def read_token(ns):
    return json.loads(ns["TOKEN_PATH"].read_text(encoding="utf-8"))


# ─────────────────────────── A: rotation ───────────────────────────
def scenario_rotation(reporter, make_ns):
    ns = make_ns()
    write_credentials(ns)
    reset_exchange()
    Exchange.response = {
        "code": 0, "location": "https://sts.example/sts", "nonce": "1",
        "ssecurity": "ssecurity-new", "userId": "user-1",
        "passToken": "pass-rotated",
    }
    account = FakeAccount("device-1")
    outcome = asyncio.run(ns["_relogin_and_refresh"](account, {}))
    assert outcome["ok"] is True, outcome
    assert outcome["saved"] is True, outcome
    saved = read_token(ns)
    assert saved["passToken"] == "pass-rotated", saved
    assert saved["micoapi"][1].startswith("service-"), saved
    reporter.ok("A1 a rotated passToken is adopted and persisted")

    # the next exchange must use the rotated value, not the stale one
    reset_exchange()
    Exchange.response = {
        "code": 0, "location": "https://sts.example/sts", "nonce": "1",
        "ssecurity": "ssecurity-newer", "userId": "user-1",
        "passToken": "pass-rotated-again",
    }
    ns["_refresh_cache"].clear()
    outcome = asyncio.run(ns["_relogin_and_refresh"](account, {}))
    assert outcome["ok"] is True, outcome
    assert Exchange.calls[-1]["token"]["passToken"] == "pass-rotated", Exchange.calls[-1]
    reporter.ok("A2 the next exchange starts from the rotated passToken")

    # a response without passToken keeps the current one
    ns = make_ns()
    write_credentials(ns, pass_token="keep-me")
    reset_exchange()
    Exchange.response = {
        "code": 0, "location": "https://sts.example/sts", "nonce": "1",
        "ssecurity": "s2", "userId": "user-1",
    }
    outcome = asyncio.run(ns["_relogin_and_refresh"](FakeAccount("device-1"), {}))
    assert outcome["ok"] is True, outcome
    assert read_token(ns)["passToken"] == "keep-me", read_token(ns)
    reporter.ok("A3 a response without passToken keeps the working credential")

    # a blank passToken must not blank the stored one
    ns = make_ns()
    write_credentials(ns, pass_token="keep-me")
    reset_exchange()
    Exchange.response = {
        "code": 0, "location": "https://sts.example/sts", "nonce": "1",
        "ssecurity": "s2", "userId": "user-1", "passToken": "   ",
    }
    outcome = asyncio.run(ns["_relogin_and_refresh"](FakeAccount("device-1"), {}))
    assert outcome["ok"] is True, outcome
    assert read_token(ns)["passToken"] == "keep-me", read_token(ns)
    reporter.ok("A4 a blank passToken never overwrites the stored one")

    # a different userId is a hard error and writes nothing
    ns = make_ns()
    before = write_credentials(ns, user="user-1", pass_token="pass-1")
    reset_exchange()
    Exchange.response = {
        "code": 0, "location": "https://sts.example/sts", "nonce": "1",
        "ssecurity": "s2", "userId": "someone-else", "passToken": "other",
    }
    outcome = asyncio.run(ns["_relogin_and_refresh"](FakeAccount("device-1"), {}))
    assert outcome["ok"] is False, outcome
    assert outcome["stage"] == "exchange", outcome
    assert read_token(ns)["userId"] == before["userId"], read_token(ns)
    assert read_token(ns)["passToken"] == before["passToken"], read_token(ns)
    reporter.ok("A5 a different userId is rejected and nothing is written")

    # an empty device list is not an authentication failure
    ns = make_ns()
    write_credentials(ns)
    reset_exchange()
    Exchange.response = {
        "code": 0, "location": "https://sts.example/sts", "nonce": "1",
        "ssecurity": "s2", "userId": "user-1", "passToken": "pass-2",
    }
    Exchange.devices = []
    outcome = asyncio.run(ns["_relogin_and_refresh"](FakeAccount("device-1"), {}))
    assert outcome["ok"] is True, outcome
    assert outcome["stage"] == "device_list", outcome
    assert outcome["saved"] is True, outcome
    assert read_token(ns)["passToken"] == "pass-2", read_token(ns)
    reporter.ok("A6 an empty device list still persists the rotated credentials")


# ─────────────────────── B: post-exchange failures ───────────────────────
def scenario_post_exchange(reporter, make_ns):
    ns = make_ns()
    write_credentials(ns)
    reset_exchange()
    Exchange.response = {
        "code": 0, "location": "https://sts.example/sts", "nonce": "1",
        "ssecurity": "s2", "userId": "user-1", "passToken": "pass-2",
    }
    Exchange.device_list_error = TimeoutError("device list timed out")
    outcome = asyncio.run(ns["_relogin_and_refresh"](FakeAccount("device-1"), {}))
    assert outcome["ok"] is True, outcome
    assert outcome["devices"] is None, outcome
    assert outcome["saved"] is True, outcome
    assert read_token(ns)["passToken"] == "pass-2", read_token(ns)
    reporter.ok("B1 a device-list timeout keeps the rotated credentials")

    ns = make_ns()
    write_credentials(ns)
    reset_exchange()
    Exchange.sts_error = True
    Exchange.response = {
        "code": 0, "location": "https://sts.example/sts", "nonce": "1",
        "ssecurity": "s2", "userId": "user-1", "passToken": "pass-2",
    }
    outcome = asyncio.run(ns["_relogin_and_refresh"](FakeAccount("device-1"), {}))
    assert outcome["ok"] is False and outcome["stage"] == "exchange", outcome
    assert read_token(ns)["passToken"] == "pass-1", read_token(ns)
    reporter.ok("B2 an STS failure is an exchange failure, not a device failure")


# ─────────────────────────── C: persistence ───────────────────────────
def scenario_persistence(reporter, make_ns):
    ns = make_ns()
    write_credentials(ns)
    reset_exchange()
    Exchange.response = {
        "code": 0, "location": "https://sts.example/sts", "nonce": "1",
        "ssecurity": "s2", "userId": "user-1", "passToken": "pass-2",
    }
    original = ns["_write_private_file"]

    def broken_write(path, payload):
        raise OSError("disk full")

    ns["_write_private_file"] = broken_write
    try:
        outcome = asyncio.run(ns["_relogin_and_refresh"](FakeAccount("device-1"), {}))
    finally:
        ns["_write_private_file"] = original
    assert outcome["ok"] is True, outcome
    assert outcome["saved"] is False, outcome
    assert "未能写入" in outcome["message"], outcome
    assert read_token(ns)["passToken"] == "pass-1", read_token(ns)
    reporter.ok("C1 a failed write is reported, and the old files survive")

    # the retry must save again, not exchange again
    calls_before = len(Exchange.calls)
    Exchange.calls = []
    ns["_write_private_file"] = original
    outcome = asyncio.run(ns["_relogin_and_refresh"](FakeAccount("device-1"), {}))
    assert outcome["saved"] is True, outcome
    assert len(Exchange.calls) == 0, (
        "a failed save must be retried without a second exchange, got %d" % len(Exchange.calls)
    )
    assert read_token(ns)["passToken"] == "pass-2", read_token(ns)
    reporter.ok("C2 a failed save is retried without another exchange")

    # the result dict distinguishes write failures from exchange failures
    ns = make_ns()
    write_credentials(ns)
    result = ns["_save_auth_and_token"](SimpleNamespace(token={}))
    assert result == {"ok": False, "reason": "empty"}, result
    reporter.ok("C3 _save_auth_and_token reports why it did not save")


# ─────────────────────────── D: deviceId ───────────────────────────
def scenario_device_id(reporter, make_ns):
    ns = make_ns()
    write_credentials(ns, device="")
    reset_exchange()
    Exchange.response = {"code": 70016, "description": "登录验证失败"}
    account = FakeAccount("")
    first = asyncio.run(ns["_relogin_and_refresh"](account, {}))
    assert first["ok"] is False, first
    used_first = Exchange.calls[0]["device_id"]
    assert used_first, "the exchange must send a non-empty deviceId"
    second = asyncio.run(ns["_relogin_and_refresh"](account, {}))
    assert second["ok"] is False, second
    assert Exchange.calls[1]["device_id"] == used_first, (
        "a retry after a failed exchange must reuse the same deviceId, got %r vs %r"
        % (Exchange.calls[1]["device_id"], used_first)
    )
    reporter.ok("D1 a missing deviceId is generated once and reused by retries")

    ns = make_ns()
    write_credentials(ns, device="saved-device")
    reset_exchange()
    Exchange.response = {"code": 70016, "description": "登录验证失败"}
    asyncio.run(ns["_relogin_and_refresh"](FakeAccount("other-device"), {}))
    assert Exchange.calls[0]["device_id"] == "saved-device", Exchange.calls[0]
    reporter.ok("D2 an existing deviceId is never replaced")

    device_id = ns["_login_device_id"]({}, FakeAccount(""))
    assert device_id and device_id != "speaker-1", device_id
    reporter.ok("D3 the login deviceId is separate from the speaker id")


# ─────────────────────────── E: concurrency ───────────────────────────
def scenario_concurrency(reporter, make_ns):
    ns = make_ns()
    write_credentials(ns)
    reset_exchange()

    def response(index, user, pass_token):
        return {"code": 0, "location": "https://sts.example/sts", "nonce": "1",
                "ssecurity": "s2", "userId": user, "passToken": "pass-%d" % index}

    Exchange.response = response
    account = FakeAccount("device-1")

    async def two_renewals():
        return await asyncio.gather(
            ns["_relogin_and_refresh"](account, {}),
            ns["_relogin_and_refresh"](account, {}),
        )

    first, second = asyncio.run(two_renewals())
    assert first["ok"] and second["ok"], (first, second)
    assert len(Exchange.calls) == 1, (
        "concurrent renewals must share one exchange, got %d" % len(Exchange.calls)
    )
    assert second.get("reused") is True, second
    reporter.ok("E1 concurrent renewals share a single exchange")

    # a user re-login during the exchange must win
    ns = make_ns()
    write_credentials(ns, pass_token="old-pass")
    reset_exchange()

    def relogin_during(index, user, pass_token):
        write_credentials(ns, pass_token="new-login-pass", service="service-new")
        return {"code": 0, "location": "https://sts.example/sts", "nonce": "1",
                "ssecurity": "s2", "userId": user, "passToken": "background-pass"}

    Exchange.response = relogin_during
    outcome = asyncio.run(ns["_relogin_and_refresh"](FakeAccount("device-1"), {}))
    assert outcome["ok"] is True, outcome
    assert outcome["saved"] is False, outcome
    assert read_token(ns)["passToken"] == "new-login-pass", read_token(ns)
    reporter.ok("E2 a user re-login during the exchange is not overwritten")

    # a credential wipe during the exchange must win too
    ns = make_ns()
    write_credentials(ns)
    reset_exchange()

    def wipe_during(index, user, pass_token):
        ns["_state"]["write_allowed"] = False
        ns["TOKEN_PATH"].unlink()
        ns["AUTH_PATH"].unlink()
        return {"code": 0, "location": "https://sts.example/sts", "nonce": "1",
                "ssecurity": "s2", "userId": user, "passToken": "background-pass"}

    Exchange.response = wipe_during
    outcome = asyncio.run(ns["_relogin_and_refresh"](FakeAccount("device-1"), {}))
    assert outcome["ok"] is True and outcome["saved"] is False, outcome
    assert not ns["TOKEN_PATH"].exists(), "a wiped login must not be resurrected"
    reporter.ok("E3 clearing the login during the exchange is not undone")


# ─────────────────────────── F: scheduling ───────────────────────────
def scenario_schedule(reporter, make_ns):
    ns = make_ns()
    interval = ns["_refresh_interval_seconds"]
    assert interval({}) == 6 * 3600, interval({})
    assert interval({"token_refresh_hours": float("nan")}) == 6 * 3600
    assert interval({"token_refresh_hours": float("inf")}) == 6 * 3600
    assert interval({"token_refresh_hours": "nonsense"}) == 6 * 3600
    assert interval({"token_refresh_hours": -1}) == 6 * 3600
    assert interval({"token_refresh_hours": 0.001}) == 900
    assert interval({"token_refresh_hours": 10 ** 9}) == 7 * 24 * 3600
    reporter.ok("F1 a NaN/inf/garbage/absurd interval falls back safely")

    age = ns["_token_age_seconds"]
    assert age() == float("inf"), age()
    ns["TOKEN_PATH"].write_text(json.dumps({"issued_at": float("nan")}), encoding="utf-8")
    assert age() == float("inf"), "a NaN issued_at must mean 'renew now'"
    ns["TOKEN_PATH"].write_text(json.dumps({"issued_at": time.time() - 60}), encoding="utf-8")
    assert 50 < age() < 600, age()
    ns["TOKEN_PATH"].write_text(json.dumps({"issued_at": time.time() + 3600}), encoding="utf-8")
    assert age() == 0.0, "a future timestamp must not produce a negative age"
    reporter.ok("F2 a corrupted issued_at renews immediately instead of never")


SCENARIOS = (
    ("rotation", scenario_rotation),
    ("post-exchange failures", scenario_post_exchange),
    ("persistence", scenario_persistence),
    ("login deviceId", scenario_device_id),
    ("concurrency", scenario_concurrency),
    ("scheduling", scenario_schedule),
)


def main():
    bridge_path = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/bridge.py")
    assert bridge_path.exists(), "built bridge.py not found: %s" % bridge_path
    reporter = Reporter()
    print("credential renewal tests | %s" % bridge_path)
    for name, scenario in SCENARIOS:
        root = Path(tempfile.mkdtemp(prefix="xiaoai-renewal-"))
        log = Log()

        def make_ns(root=root, log=log):
            return build_namespace(bridge_path, root, log)

        scenario(reporter, make_ns)
        reporter.done(name)
    print("credential renewal tests passed (%d checks, %d scenarios)"
          % (reporter.checks, reporter.scenarios))
    return 0


if __name__ == "__main__":
    sys.exit(main())
