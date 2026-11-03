#!/usr/bin/env python3
"""Regression for scheduled voice rules -> Home Assistant automations.

The rules page documents ``{"type":"schedule","schedule_time":"23:00:00",
"schedule_days":[1,2,3,4,5,6,7]}`` while the pinned upstream helper only accepted
``HH:MM`` and called ``.lower()`` on the weekday value. Both are exercised here
against the *generated* bridge.py (no fake helper for the code under test).
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import sys

FUNCTIONS = ("_normalize_schedule_time", "_normalize_schedule_weekdays", "_create_ha_automation")
TABLES = ("_SCHEDULE_WEEKDAY_ORDER", "_SCHEDULE_WEEKDAY_ALIASES")


class FakeHA:
    def __init__(self):
        self.created = []

    async def create_automation(self, automation_id, config, session):
        self.created.append(config)
        return True

    async def reload_automations(self, session):
        return True


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
    namespace = {"re": __import__("re"), "time": __import__("time"),
                 "aiohttp": __import__("aiohttp"), "secrets": __import__("secrets"),
                 "uuid": __import__("uuid"), "json": __import__("json"),
                 "log": __import__("logging").getLogger("t"),
                 "_AUTOMATION_PREFIX": "xiaoai_bridge_"}
    module = ast.Module(body=wanted, type_ignores=[])
    exec(compile(module, str(bridge_path), "exec"), namespace)
    return namespace


def main() -> int:
    bridge = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "bridge.py")
    ns = load(bridge)
    normalize_time = ns["_normalize_schedule_time"]
    normalize_days = ns["_normalize_schedule_weekdays"]
    create = ns["_create_ha_automation"]
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

    print()
    if failures:
        print(f"schedule automation tests FAILED ({checks} checks, {len(failures)} failed)")
        return 1
    print(f"schedule automation tests passed ({checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
