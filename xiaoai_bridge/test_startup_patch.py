"""Isolated checks for the patched bridge's QR-only startup path."""

import ast
import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace


source_path = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/bridge.py")
tree = ast.parse(source_path.read_text(encoding="utf-8"))
assert 'IntentParser(load_config().get("intent_rules", [])).parse(query)' in source_path.read_text(encoding="utf-8")
intent_node = next(
    node for node in tree.body
    if isinstance(node, ast.ClassDef) and node.name == "IntentParser"
)
intent_namespace = {"re": re}
exec(compile(ast.Module(body=[intent_node], type_ignores=[]), str(source_path), "exec"), intent_namespace)
multi_entity_ids = ["switch.one", "switch.two", "switch.three", "switch.four"]
parsed_action = intent_namespace["IntentParser"]([{
    "pattern": "打开客厅灯",
    "action": {
        "domain": "switch", "service": "turn_on",
        "entity_id": multi_entity_ids,
    },
}]).parse("打开客厅灯")
assert parsed_action["entity_id"] == multi_entity_ids
needed = {"_set_token", "_load_saved_xiaomi_identity", "bridge_loop", "post_config"}
nodes = {
    node.name: node
    for node in tree.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in needed
}
assert set(nodes) == needed
nodes["post_config"].decorator_list = []


class Logger:
    def __init__(self):
        self.messages = []

    def info(self, message, *args):
        self.messages.append(message % args if args else message)

    warning = info
    error = info


class FakeSession:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeAccount:
    created = []

    def __init__(self, session, username, password, token_store):
        assert token_store is None, "MiService must not own/delete the saved token file"
        self.token = None
        self.created.append(self)

    async def login(self, sid):
        raise AssertionError("cached valid micoapi must not re-login")


class FakeMiNAService:
    calls = 0

    def __init__(self, account):
        self.account = account

    async def device_list(self):
        self.__class__.calls += 1
        if self.account.token["micoapi"][1] != "valid-service":
            self.account.token = None  # emulate MiService's 401 handling
            raise RuntimeError("HTTP 401")
        return [{"deviceID": "speaker-1", "hardware": "L05C", "name": "speaker"}]


class FakeHAClient:
    calls = 0

    def __init__(self, url, token):
        assert url == "http://homeassistant:80"
        assert token == "ha-secret"

    async def test_connection(self, session):
        self.__class__.calls += 1
        namespace["bridge_state"]["running"] = False
        return True


miservice = ModuleType("miservice")
miservice.MiAccount = FakeAccount
miservice.MiNAService = FakeMiNAService
miaccount = ModuleType("miservice.miaccount")
miaccount.get_random = lambda length: "R" * length
sys.modules["miservice"] = miservice
sys.modules["miservice.miaccount"] = miaccount


with tempfile.TemporaryDirectory() as temp_dir:
    root = Path(temp_dir)
    token_path = root / ".mi.token"
    auth_path = root / "auth.json"
    cfg = {
        "xiaomi": {"username": "", "password": "", "cookie_text": "", "device_did": "speaker-1"},
        "homeassistant": {"url": "http://homeassistant:80", "token": "ha-secret"},
        "intent_rules": [],
        "poll_interval_seconds": 2,
    }
    namespace = {
        "TOKEN_PATH": token_path,
        "AUTH_PATH": auth_path,
        "CONFIG_PATH": root / "config.yaml",
        "COOKIE_TEMPLATE": "deviceId={device_id}; serviceToken={service_token}; userId={user_id}",
        "json": json,
        "log": Logger(),
        "load_config": lambda: cfg,
        "save_config": lambda new_cfg: saved_configs.append(new_cfg),
        "aiohttp": SimpleNamespace(ClientSession=FakeSession),
        "asyncio": asyncio,
        "IntentParser": lambda rules: object(),
        "HAClient": FakeHAClient,
        "_get_cookie": lambda config: None,
        "_save_auth_and_token": lambda account: None,
        "_refresh_micoapi_token": None,
        "TestHARequest": object,
        "SaveConfigRequest": object,
        "time": __import__("time"),
        "bridge_state": {"running": True, "connected": False, "error": None},
    }
    saved_configs = []
    for name in ("_set_token", "_load_saved_xiaomi_identity", "post_config", "bridge_loop"):
        exec(compile(ast.Module(body=[nodes[name]], type_ignores=[]), str(source_path), "exec"), namespace)

    async def refresh_must_not_run(*args):
        raise AssertionError("valid saved token must not be exchanged again")

    namespace["_refresh_micoapi_token"] = refresh_must_not_run
    original = {
        "userId": "user-1",
        "passToken": "pass-1",
        "deviceId": "account-device",
        "micoapi": ["security", "valid-service"],
    }
    token_path.write_text(json.dumps(original), encoding="utf-8")
    auth_path.write_text(json.dumps({key: original[key] for key in ("userId", "passToken", "deviceId")}), encoding="utf-8")

    asyncio.run(namespace["bridge_loop"]())
    assert FakeMiNAService.calls == 1
    assert FakeHAClient.calls == 1
    assert namespace["bridge_state"]["error"] is None
    assert namespace["bridge_state"]["devices"][0]["deviceID"] == "speaker-1"
    assert json.loads(token_path.read_text(encoding="utf-8")) == original
    assert any("已使用保存的 micoapi Token 登录" in message for message in namespace["log"].messages)

    cfg["xiaomi"]["cookie_text"] = "userId=fresh; passToken=fresh-pass; serviceToken=fresh-service"
    stale_page = {**cfg, "xiaomi": {**cfg["xiaomi"], "cookie_text": ""}}
    result = asyncio.run(namespace["post_config"](SimpleNamespace(config=stale_page)))
    assert result["ok"] is True
    assert saved_configs[-1]["xiaomi"]["cookie_text"] == cfg["xiaomi"]["cookie_text"]

    async def failed_refresh(*args):
        raise RuntimeError("micoapi login rejected")

    cfg["xiaomi"]["cookie_text"] = ""
    namespace["_refresh_micoapi_token"] = failed_refresh
    namespace["bridge_state"] = {"running": True, "connected": False, "error": None}
    original["micoapi"] = ["security", "expired-service"]
    token_path.write_text(json.dumps(original), encoding="utf-8")
    asyncio.run(namespace["bridge_loop"]())
    assert "重新扫码" in namespace["bridge_state"]["error"]
    assert json.loads(token_path.read_text(encoding="utf-8")) == original

print("startup patch tests passed")
