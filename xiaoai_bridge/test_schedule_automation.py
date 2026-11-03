#!/usr/bin/env python3
"""Regression for scheduled voice rules -> Home Assistant automations.

The rules page documents ``{"type":"schedule","schedule_time":"23:00:00",
"schedule_days":[1,2,3,4,5,6,7]}`` while the pinned upstream helper only accepted
``HH:MM`` and called ``.lower()`` on the weekday value. Both are exercised here
against the *generated* bridge.py (no fake helper for the code under test).

The schedules page can create a task without the voice path; that endpoint is
exercised here too (same ``_create_ha_automation`` helper, with the form
validation in front of it), plus the config-id/entity-id split of the list.
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import sys
import types

FUNCTIONS = (
    "_normalize_schedule_time",
    "_normalize_schedule_weekdays",
    "_schedule_form_days",
    "_schedule_form_action",
    "_automation_entries",
    "_schedule_ha_target_error",
    "_automation_config_state",
    "_create_ha_automation",
    "create_automation",
)
TABLES = ("_SCHEDULE_WEEKDAY_ORDER", "_SCHEDULE_WEEKDAY_ALIASES",
          "_SCHEDULE_DAY_WORDS", "_SCHEDULE_TARGET_OPTIONAL_DOMAINS",
          "_SCHEDULE_CROSS_DOMAIN_ALLOWED")

CURRENT_CONFIG = {"homeassistant": {"url": "http://ha.local:8123", "token": "ha-token"}}
CREATED_HA = []
ROUTES = {}


class FakeHA:
    create_ok = True
    reload_ok = True

    def __init__(self):
        self.created = []
        self.base = "http://ha.local:8123"
        self.headers = {"Authorization": "Bearer ha-token"}

    async def create_automation(self, automation_id, config, session):
        self.created.append(config)
        return type(self).create_ok

    async def reload_automations(self, session):
        return type(self).reload_ok

    async def list_automation_states(self, session):
        return []


class HttpError(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class StubApp:
    def post(self, path):
        def decorator(func):
            func.route = path
            return func

        return decorator


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


class StubSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        for prefix, (status, payload) in ROUTES.items():
            if str(url).startswith(prefix):
                return FakeResponse(status, payload)
        return FakeResponse(404, None)


def _load_config():
    return json.loads(json.dumps(CURRENT_CONFIG))


def _new_ha(url, token):
    ha = FakeHA()
    ha.base = str(url).rstrip("/")
    ha.headers = {"Authorization": "Bearer " + str(token)}
    CREATED_HA.append(ha)
    return ha


def load(bridge_path: pathlib.Path):
    source = bridge_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = [node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS]
    tables = [node for node in tree.body
              if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id in TABLES for target in node.targets)]
    missing = [name for name in FUNCTIONS if name not in {node.name for node in functions}]
    assert not missing, f"bridge.py is missing {missing}"
    wanted = tables + functions
    namespace = {
        "re": __import__("re"), "time": __import__("time"),
        "aiohttp": types.SimpleNamespace(
            ClientSession=StubSession,
            ClientTimeout=__import__("aiohttp").ClientTimeout,
        ),
        "secrets": __import__("secrets"), "uuid": __import__("uuid"),
        "json": __import__("json"),
        "log": __import__("logging").getLogger("t"),
        "_AUTOMATION_PREFIX": "xiaoai_bridge_",
        "HTTPException": HttpError,
        "app": StubApp(),
        "BaseModel": object,
        "load_config": _load_config,
        "HAClient": _new_ha,
        "ScheduleAutomationRequest": object,
    }
    module = ast.Module(body=wanted, type_ignores=[])
    exec(compile(module, str(bridge_path), "exec"), namespace)
    return namespace


def main() -> int:
    bridge = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "bridge.py")
    ns = load(bridge)
    normalize_time = ns["_normalize_schedule_time"]
    normalize_days = ns["_normalize_schedule_weekdays"]
    form_days = ns["_schedule_form_days"]
    form_action = ns["_schedule_form_action"]
    automation_entries = ns["_automation_entries"]
    create = ns["_create_ha_automation"]
    create_endpoint = ns["create_automation"]
    checks = 0
    failures = []

    def check(name, ok, detail=""):
        nonlocal checks
        checks += 1
        if not ok:
            failures.append(f"{name}{(' :: ' + str(detail)) if detail else ''}")
            print(f"  FAIL {name} {detail}")

    def eq(name, actual, expected):
        check(name, actual == expected, f"expected {expected!r}, got {actual!r}")

    eq("HH:MM normalized", normalize_time("23:00"), "23:00:00")
    eq("HH:MM:SS kept", normalize_time("07:30:05"), "07:30:05")
    eq("single digit hour padded", normalize_time("7:05"), "07:05:00")
    eq("invalid hour refused", normalize_time("25:00"), "")
    eq("invalid minute refused", normalize_time("12:99"), "")
    eq("garbage refused", normalize_time("晚上八点"), "")

    eq("daily means no condition", normalize_days("daily"), [])
    eq("Chinese daily", normalize_days("每天"), [])
    eq("weekdays", normalize_days("weekdays"), ["mon", "tue", "wed", "thu", "fri"])
    eq("weekends", normalize_days("周末"), ["sat", "sun"])
    eq("integer array as documented", normalize_days([1, 2, 3, 4, 5, 6, 7]),
       ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
    eq("single integer", normalize_days([1]), ["mon"])
    eq("string list", normalize_days("mon,wed,fri"), ["mon", "wed", "fri"])
    eq("Chinese weekday", normalize_days("周三,周五"), ["wed", "fri"])
    eq("out of range integers ignored", normalize_days([0, 8]), [])
    eq("mixed list keeps order", normalize_days(["fri", 1]), ["mon", "fri"])

    # ── strict weekday parsing for the manual form (audit P1-3) ──
    eq("form days: daily", form_days("daily"), (None, ""))
    eq("form days: 每天", form_days("每天"), (None, ""))
    eq("form days: weekdays", form_days("工作日"),
       (["mon", "tue", "wed", "thu", "fri"], ""))
    eq("form days: weekends", form_days("周末"), (["sat", "sun"], ""))
    eq("form days: integer list", form_days("1,3,5"), (["mon", "wed", "fri"], ""))
    eq("form days: names", form_days("mon,wed"), (["mon", "wed"], ""))
    eq("form days: empty means daily", form_days(""), (None, ""))
    days, error = form_days("8")
    check("form days: out-of-range integer is refused", days is None and "8" in error, error)
    days, error = form_days("1,8")
    check("form days: mixed invalid token is refused", days is None and "8" in error, error)
    days, error = form_days("mondayy")
    check("form days: unknown weekday name is refused", days is None and "mondayy" in error, error)

    async def run(time_value, days_value, domain="light", service="turn_on", data=None):
        ha = FakeHA()
        automation_id, ok = await create(
            ha, domain, service, {"entity_id": "light.a"} if data is None else data,
            time_value, days_value, None)
        return automation_id, ok, ha.created

    automation_id, ok, created = asyncio.run(run("23:00:00", [1, 2, 3, 4, 5, 6, 7]))
    check("documented schedule creates the automation", ok and automation_id.startswith("xiaoai_bridge_"))
    eq("trigger uses HH:MM:SS", created[0]["trigger"][0]["at"], "23:00:00")
    eq("weekday condition from the integer array", created[0]["condition"][0]["weekday"],
       ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])

    _, ok, created = asyncio.run(run("23:00", "weekdays"))
    check("HH:MM with weekdays is accepted", ok)
    eq("weekdays condition", created[0]["condition"][0]["weekday"],
       ["mon", "tue", "wed", "thu", "fri"])

    _, ok, created = asyncio.run(run("07:05", None))
    check("daily schedule omits the condition", ok and "condition" not in created[0])

    _, ok, created = asyncio.run(run("25:00", None))
    check("invalid time is refused", not ok and not created)

    # audit P1-2: rapid (sequential) creations in the same second must not share a
    # config id; this is a uniqueness check, not a concurrency stress test.
    ids = set()
    for _ in range(25):
        same_second_id, _ok, _created = asyncio.run(run("07:30", None))
        ids.add(same_second_id)
    check("rapid sequential creations get unique config ids", len(ids) == 25, len(ids))

    # audit P2-1: no entity -> no empty target
    _, ok, created = asyncio.run(run("07:30", None, domain="notify", service="notify", data={}))
    check("a target-less action omits target", ok and "target" not in created[0]["action"][0],
          created[0]["action"][0] if created else None)

    # ── config id vs entity id (audit P1-1) ──
    states = [
        {"entity_id": "automation.xiao_ai_qiao_jie_0730_light_a", "state": "on",
         "attributes": {"id": "xiaoai_bridge_1790000000_abcd1234",
                        "friendly_name": "小爱桥接-07:30 light.a",
                        "last_triggered": "2026-11-03T07:30:00"}},
        {"entity_id": "automation.other", "state": "off",
         "attributes": {"id": "not_ours", "friendly_name": "别人的"}},
        {"entity_id": "automation.foreign", "state": "on", "attributes": {"friendly_name": "x"}},
        {"entity_id": "light.a", "state": "on", "attributes": {"id": "xiaoai_bridge_x"}},
        "garbage",
    ]
    entries = automation_entries(states)
    eq("list keeps only bridge automations", len(entries), 1)
    eq("list exposes the config id", entries[0]["config_id"], "xiaoai_bridge_1790000000_abcd1234")
    eq("list keeps the real entity id", entries[0]["entity_id"],
       "automation.xiao_ai_qiao_jie_0730_light_a")
    eq("list keeps the alias", entries[0]["alias"], "小爱桥接-07:30 light.a")
    eq("states without attributes.id are ignored", automation_entries([
        {"entity_id": "automation.no_id", "state": "on",
         "attributes": {"last_triggered": None, "mode": "single", "current": 0}}]), [])

    # ── the schedules-page form: validation helper ──
    data, error = form_action("light", "turn_on", "light.a")
    eq("form action keeps the entity", data, {"entity_id": "light.a"})
    check("valid form has no error", error == "")
    data, error = form_action("Light", "Turn_On", "light.a")
    check("form action lowercases domain/service",
          error == "" and data == {"entity_id": "light.a"}, error)
    data, error = form_action("light!", "turn_on", "")
    check("bad domain is refused", data == {} and "域" in error)
    data, error = form_action("light", "turn on", "")
    check("bad service is refused", data == {} and "服务" in error)
    data, error = form_action("light", "turn_on", "light")
    check("entity without a dot is refused", data == {} and "实体" in error)
    data, error = form_action("light", "turn_on", "")
    check("device domain without an entity is refused", data == {} and "必须填写实体" in error)
    data, error = form_action("notify", "notify", "")
    check("notify may omit the entity", error == "" and data == {})
    data, error = form_action("light", "turn_on", "switch.plug_1")
    check("a service cannot target another domain", data == {} and "不能作用于" in error)
    data, error = form_action("homeassistant", "turn_on", "switch.plug_1")
    check("homeassistant may target any domain",
          error == "" and data == {"entity_id": "switch.plug_1"}, error)

    # ── the schedules-page form: POST /api/automations ──
    def call(req):
        return asyncio.run(create_endpoint(req))

    def form(time_value, days="daily", domain="light", service="turn_on",
             entity="light.a"):
        return types.SimpleNamespace(time=time_value, days=days, domain=domain,
                                     service=service, entity_id=entity)

    def expect_error(req, label, hint, status=400):
        try:
            result = call(req)
        except HttpError as exc:
            check(label, exc.status_code == status and hint in str(exc.detail),
                  f"got HTTP {exc.status_code}: {exc.detail}")
            return
        check(label, False, f"expected HTTPException, got {result}")

    ROUTES.clear()
    CREATED_HA.clear()
    response = call(form("07:30", "1,3,5"))
    check("form endpoint creates the automation", response.get("ok") is True, response)
    check("form endpoint returns the automation id",
          str(response.get("automation_id", "")).startswith("xiaoai_bridge_"), response)
    check("form endpoint reports a successful reload", response.get("reloaded") is True, response)
    eq("form endpoint message names the time", response.get("msg"), "已创建定时任务 07:30")
    check("form endpoint called HA once", len(CREATED_HA) == 1, len(CREATED_HA))
    created = CREATED_HA[-1].created[-1]
    eq("form alias", created["alias"], "小爱桥接-07:30 light.a")
    eq("form trigger", created["trigger"][0], {"platform": "time", "at": "07:30:00"})
    eq("form action", created["action"][0],
       {"service": "light.turn_on", "target": {"entity_id": "light.a"}})
    eq("form weekday condition", created["condition"][0]["weekday"], ["mon", "wed", "fri"])
    check("form creates no unsaved state", created["mode"] == "single")

    expect_error(form("25:00"), "form refuses an invalid time", "时间格式")
    expect_error(form("07:30", days="8"), "form refuses an invalid weekday", "8")
    expect_error(form("07:30", days="1,8"), "form refuses a mixed invalid weekday", "8")
    expect_error(form("07:30", domain="bad-domain!"), "form refuses a bad domain", "域")
    expect_error(form("07:30", service="turn on"), "form refuses a bad service", "服务")
    expect_error(form("07:30", entity="light"), "form refuses a bad entity", "实体")
    expect_error(form("07:30", entity=""), "form refuses a device service without an entity",
                 "必须填写实体")
    expect_error(form("07:30", entity="switch.plug_1"),
                 "form endpoint refuses a service/entity domain mismatch", "不能作用于", 400)

    # audit P2-3: the target is checked against HA's own service/entity lists
    ROUTES["http://ha.local:8123/api/services"] = (
        200, [{"domain": "light", "services": {"turn_on": {}, "turn_off": {}}},
              {"domain": "switch", "services": {"turn_on": {}}}])
    expect_error(form("07:30", service="turn_onn", entity="light.a"),
                 "form refuses an unknown HA service", "服务", 400)
    expect_error(form("07:30", entity="light.ghost"),
                 "form refuses an unknown entity", "找不到实体", 400)
    ROUTES["http://ha.local:8123/api/states/light.a"] = (200, {"entity_id": "light.a"})
    response = call(form("07:30", entity="light.a"))
    check("form accepts a service/entity HA knows", response.get("ok") is True, response)

    # audit P2-4: created but reload failed must not be reported as "not created"
    ROUTES["http://ha.local:8123/api/config/automation/config/"] = (200, {"alias": "x"})
    FakeHA.reload_ok = False
    try:
        response = call(form("07:30", entity="light.a"))
        check("reload failure after a saved config is reported as a warning",
              response.get("ok") is True and response.get("reloaded") is False, response)
    finally:
        FakeHA.reload_ok = True
    ROUTES.pop("http://ha.local:8123/api/config/automation/config/", None)

    # audit P2-4: nothing saved and reload failed -> a real failure
    FakeHA.reload_ok = False
    try:
        expect_error(form("07:30", entity="light.a"),
                     "a failed creation without a saved config returns 500", "创建失败", 500)
    finally:
        FakeHA.reload_ok = True

    # audit P2-4 / P2-B: an unconfirmable result must not be reported as "not created"
    ROUTES["http://ha.local:8123/api/config/automation/config/"] = (500, None)
    FakeHA.reload_ok = False
    try:
        expect_error(form("07:30", entity="light.a"),
                     "an unconfirmable creation returns 504 with a check message",
                     "无法确认", 504)
    finally:
        FakeHA.reload_ok = True
    ROUTES.pop("http://ha.local:8123/api/config/automation/config/", None)

    CURRENT_CONFIG["homeassistant"] = {"url": "", "token": ""}
    expect_error(form("07:30"), "form refuses when HA is not configured", "HA 未配置", 400)
    CURRENT_CONFIG["homeassistant"] = {"url": "http://ha.local:8123", "token": "ha-token"}

    print()
    if failures:
        print(f"schedule automation tests FAILED ({checks} checks, {len(failures)} failed)")
        return 1
    print(f"schedule automation tests passed ({checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
