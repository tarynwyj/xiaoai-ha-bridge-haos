#!/usr/bin/env python3
"""Regression for scheduled voice rules -> Home Assistant automations.

The rules page documents ``{"type":"schedule","schedule_time":"23:00:00",
"schedule_days":[1,2,3,4,5,6,7]}`` while the pinned upstream helper only accepted
``HH:MM`` and called ``.lower()`` on the weekday value. Both are exercised here
against the *generated* bridge.py (no fake helper for the code under test).

The schedules page can now create a task without the voice path; that endpoint is
exercised here too (same ``_create_ha_automation`` helper, with the form
validation in front of it).
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
    "_schedule_form_action",
    "_create_ha_automation",
    "create_automation",
)
TABLES = ("_SCHEDULE_WEEKDAY_ORDER", "_SCHEDULE_WEEKDAY_ALIASES")

CURRENT_CONFIG = {"homeassistant": {"url": "http://ha.local:8123", "token": "ha-token"}}
CREATED_HA = []


class FakeHA:
    def __init__(self):
        self.created = []

    async def create_automation(self, automation_id, config, session):
        self.created.append(config)
        return True

    async def reload_automations(self, session):
        return True

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


class StubSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _load_config():
    return json.loads(json.dumps(CURRENT_CONFIG))


def _new_ha(url, token):
    ha = FakeHA()
    ha.url = url
    ha.token = token
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
        "aiohttp": types.SimpleNamespace(ClientSession=StubSession),
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
    form_action = ns["_schedule_form_action"]
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

    async def run(time_value, days_value):
        ha = FakeHA()
        automation_id, ok = await create(ha, "light", "turn_on", {"entity_id": "light.a"},
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

    # ── the schedules-page form: validation helper ──
    data, error = form_action("light", "turn_on", "light.a")
    eq("form action keeps the entity", data, {"entity_id": "light.a"})
    check("valid form has no error", error == "")
    data, error = form_action("Light", "Turn_On", "")
    check("form action lowercases domain/service", error == "" and data == {})
    data, error = form_action("light!", "turn_on", "")
    check("bad domain is refused", data == {} and "域" in error)
    data, error = form_action("light", "turn on", "")
    check("bad service is refused", data == {} and "服务" in error)
    data, error = form_action("light", "turn_on", "light")
    check("entity without a dot is refused", data == {} and "实体" in error)
    data, error = form_action("", "turn_on", "")
    check("empty domain is refused", data == {} and "域" in error)

    # ── the schedules-page form: POST /api/automations ──
    def call(req):
        return asyncio.run(create_endpoint(req))

    def form(time_value, days="daily", domain="light", service="turn_on",
             entity="light.a"):
        return types.SimpleNamespace(time=time_value, days=days, domain=domain,
                                     service=service, entity_id=entity)

    CREATED_HA.clear()
    response = call(form("07:30", "1,3,5"))
    check("form endpoint creates the automation", response.get("ok") is True, response)
    check("form endpoint returns the automation id",
          str(response.get("automation_id", "")).startswith("xiaoai_bridge_"), response)
    eq("form endpoint message names the time", response.get("msg"), "已创建定时任务 07:30")
    check("form endpoint called HA once", len(CREATED_HA) == 1, len(CREATED_HA))
    created = CREATED_HA[-1].created[-1]
    eq("form alias", created["alias"], "小爱桥接-07:30 light.a")
    eq("form trigger", created["trigger"][0], {"platform": "time", "at": "07:30:00"})
    eq("form action", created["action"][0],
       {"service": "light.turn_on", "target": {"entity_id": "light.a"}})
    eq("form weekday condition", created["condition"][0]["weekday"],
       ["mon", "wed", "fri"])
    check("form creates no unsaved state", created["mode"] == "single")

    def expect_error(req, label, hint):
        try:
            result = call(req)
        except HttpError as exc:
            check(label, exc.status_code == 400 and hint in str(exc.detail),
                  f"got HTTP {exc.status_code}: {exc.detail}")
            return
        check(label, False, f"expected HTTPException, got {result}")

    expect_error(form("25:00"), "form refuses an invalid time", "时间格式")
    expect_error(form("07:30", domain="bad-domain!"), "form refuses a bad domain", "域")
    expect_error(form("07:30", service="turn on"), "form refuses a bad service", "服务")
    expect_error(form("07:30", entity="light"), "form refuses a bad entity", "实体")

    CURRENT_CONFIG["homeassistant"] = {"url": "", "token": ""}
    try:
        call(form("07:30"))
        check("form refuses when HA is not configured", False, "no error raised")
    except HttpError as exc:
        check("form refuses when HA is not configured",
              exc.status_code == 400 and "HA 未配置" in str(exc.detail), exc.detail)
    CURRENT_CONFIG["homeassistant"] = {"url": "http://ha.local:8123", "token": "ha-token"}

    print()
    if failures:
        print(f"schedule automation tests FAILED ({checks} checks, {len(failures)} failed)")
        return 1
    print(f"schedule automation tests passed ({checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
