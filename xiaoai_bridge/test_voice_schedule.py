#!/usr/bin/env python3
"""Voice-created schedule tasks: Chinese parsing, stable ids, verified creation.

Runs against the built bridge.py (patch_auth.py applied) with a fake HA client,
so no Home Assistant instance is needed.

    python xiaoai_bridge/test_voice_schedule.py <built bridge.py>
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

WANTED_FUNCS = (
    "_cn_int",
    "_parse_cn_clock",
    "_apply_period",
    "_parse_relative_offset",
    "_parse_once_date",
    "_normalize_schedule_date",
    "_schedule_now",
    "_schedule_once_at",
    "_schedule_local_naive",
    "_schedule_once_state",
    "_parse_schedule_days_cn",
    "_looks_like_schedule",
    "_pick_schedule_device",
    "_schedule_service_for",
    "_parse_schedule_command",
    "_automation_stable_id",
    "_automation_outcome",
    "_automation_exists",
    "_normalize_schedule_time",
    "_normalize_schedule_weekdays",
    "_create_ha_automation",
    "_schedule_registry_path",
    "_schedule_registry_read",
    "_schedule_registry_write",
    "_schedule_registry_add",
    "_schedule_registry_remove",
    "_schedule_next_run",
    "_schedule_listing",
    "_schedule_purge_expired",
    "_automation_entries",
)
NEEDED_ASSIGNMENTS = ("_AUTOMATION_PREFIX", "_WEEKDAY_MAP", "_automation_outcomes", "_SCHEDULE_HINTS", "_CN_DIGITS", "_CN_WEEKDAY", "_SCHEDULE_WEEKDAY_ORDER", "_SCHEDULE_INTERNAL_KEYS")

DEVICES = [
    {"entity_id": "switch.reshui", "name": "热水器", "domain": "switch", "state": "off"},
    {"entity_id": "light.keting", "name": "客厅主灯", "domain": "light", "state": "on"},
    {"entity_id": "cover.chuanglian", "name": "卧室窗帘", "domain": "cover", "state": "open"},
    {"entity_id": "media_player.tv", "name": "客厅电视", "domain": "media_player", "state": "idle"},
]


REGISTRY_PATH = __import__("os").path.join(
    __import__("tempfile").gettempdir(), "xiaoai-voice-schedules.json")


class Log:
    def __init__(self):
        self.messages = []

    def _rec(self, level, message, *args):
        self.messages.append("[%s] %s" % (level, message % args if args else message))

    def info(self, message, *args):
        self._rec("INFO", message, *args)

    def warning(self, message, *args):
        self._rec("WARNING", message, *args)

    def error(self, message, *args):
        self._rec("ERROR", message, *args)

    def has(self, needle):
        return any(needle in line for line in self.messages)


class FakeHA:
    """Records what the bridge asks HA to do."""

    def __init__(self):
        self.base = "http://ha.local:8123"
        self.headers = {}
        self.created = []
        self.reload_calls = 0
        self.states = []
        self.reload_ok = True
        self.create_ok = True
        self.list_error = None

    async def create_automation(self, automation_id, config, session=None):
        self.created.append({"id": automation_id, "config": config})
        if self.create_ok:
            self.states.append({
                "entity_id": "automation." + automation_id,
                "state": "on",
                "attributes": {"id": automation_id,
                               "friendly_name": config.get("alias", "")},
            })
        return self.create_ok

    async def reload_automations(self, session=None):
        self.reload_calls += 1
        return self.reload_ok

    async def delete_automation(self, automation_id, session=None):
        self.states = [state for state in self.states
                       if (state.get("attributes") or {}).get("id") != automation_id]
        return True

    async def list_automation_states(self, session=None):
        if self.list_error:
            raise self.list_error
        return list(self.states)


class Reporter:
    def __init__(self):
        self.checks = 0
        self.failures = []

    def ok(self, name):
        self.checks += 1

    def check(self, name, condition, detail=""):
        self.checks += 1
        if not condition:
            self.failures.append(name + ((" :: " + str(detail)) if detail else ""))
            print("  FAIL " + name + ((" :: " + str(detail)) if detail else ""))

    def eq(self, name, actual, expected):
        self.check(name, actual == expected, "expected %r, got %r" % (expected, actual))


def build_namespace(bridge: Path):
    source = bridge.read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {n.name: n for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    missing = [n for n in WANTED_FUNCS if n not in functions]
    assert not missing, "built bridge.py is missing: %s" % missing
    namespace = {"re": __import__("re"), "time": __import__("time"),
                 "log": Log(), "json": json,
                 "Path": __import__("pathlib").Path,
                 "_SCHEDULE_REGISTRY_OVERRIDE": REGISTRY_PATH}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(n in NEEDED_ASSIGNMENTS for n in names):
                exec(compile(ast.Module(body=[node], type_ignores=[]), str(bridge), "exec"),
                     namespace)
    namespace.setdefault("_automation_outcomes", {})
    # _create_ha_automation is defined twice; the later definition must win.
    order = [n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name in WANTED_FUNCS]
    for node in order:
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(bridge), "exec"),
             namespace)
    return namespace


def parse(ns, text, aliases=None):
    return ns["_parse_schedule_command"](text, DEVICES, aliases or {})


def scenario_parsing(reporter, ns):
    cases = [
        ("每天早上七点半打开热水器", "07:30", "daily", "switch.reshui", "turn_on"),
        ("每天晚上八点关掉客厅主灯", "20:00", "daily", "light.keting", "turn_off"),
        ("每周一三五晚上九点打开客厅主灯", "21:00", "1,3,5", "light.keting", "turn_on"),
        ("工作日早上七点打开热水器", "07:00", "weekdays", "switch.reshui", "turn_on"),
        ("周末下午三点打开卧室窗帘", "15:00", "weekends", "cover.chuanglian", "open_cover"),
        ("每天七点一刻打开热水器", "07:15", "daily", "switch.reshui", "turn_on"),
        ("每天早上七点打开热水器", "07:00", "daily", "switch.reshui", "turn_on"),
    ]
    for text, at, days, entity, service in cases:
        action = parse(ns, text)
        reporter.check("parsed: " + text, action is not None, "returned None")
        if not action:
            continue
        reporter.eq(text + " / time", action["schedule_time"], at)
        reporter.eq(text + " / days", action["schedule_days"], days)
        reporter.eq(text + " / entity", action["entity_id"], entity)
        reporter.eq(text + " / service", action["service"], service)

    for text in ("打开热水器", "今天天气怎么样", "每天七点半打开洗衣机"):
        reporter.check("refused: " + text, parse(ns, text) is None,
                       parse(ns, text))

    action = parse(ns, "明天八点打开热水器")
    reporter.check("明天 marks a one-shot", action is not None and action["once"] is True, action)
    if action:
        from datetime import datetime, timedelta
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        reporter.eq("明天 records tomorrow's date", action.get("once_date"), tomorrow)
        reporter.eq("明天 is stored as a one-shot rule", action["schedule_days"], "once")

    # 相对时间：十分钟后 / 半小时后 只能是一次性的绝对时间点
    from datetime import datetime as _dt2, timedelta as _td2
    before = _dt2.now()
    action = parse(ns, "十分钟后打开热水器")
    after = _dt2.now()
    reporter.check("十分钟后 is understood as a one-shot",
                   action is not None and action["once"] is True, action)
    if action:
        reporter.eq("十分钟后 is stored as a one-shot rule", action["schedule_days"], "once")
        reporter.check("十分钟后 keeps today's date",
                       action.get("once_date") in (before.strftime("%Y-%m-%d"),
                                                   after.strftime("%Y-%m-%d")),
                       action.get("once_date"))
        expected_low = (before + _td2(minutes=10)).strftime("%H:%M")
        expected_high = (after + _td2(minutes=10)).strftime("%H:%M")
        reporter.check("十分钟后 lands ten minutes ahead",
                       action["schedule_time"] in (expected_low, expected_high),
                       (action["schedule_time"], expected_low, expected_high))

    action = parse(ns, "半小时后关掉客厅主灯")
    reporter.check("半小时后 is understood too",
                   action is not None and action["once"] is True
                   and action["service"] == "turn_off", action)

    action = parse(ns, "两个小时后打开卧室窗帘")
    reporter.check("N 小时后 is understood",
                   action is not None and action["once"] is True, action)

    reporter.check("a relative phrase without a device is refused",
                   parse(ns, "十分钟后提醒我") is None)
    reporter.check("每天 is not a one-shot",
                   parse(ns, "每天八点打开热水器")["once"] is False)

    action = parse(ns, "每天八点打开主灯", aliases={"主灯": "light.keting"})
    reporter.check("an alias resolves to the entity",
                   action is not None and action["entity_id"] == "light.keting", action)

    reporter.check("schedule hints are recognised", ns["_looks_like_schedule"]("每天八点开灯"))
    reporter.check("plain chat is not a schedule", not ns["_looks_like_schedule"]("你好"))


def scenario_stable_ids(reporter, ns):
    stable = ns["_automation_stable_id"]
    first = stable("07:30", [], "switch.reshui", "switch", "turn_on")
    second = stable("07:30", [], "switch.reshui", "switch", "turn_on")
    other = stable("07:31", [], "switch.reshui", "switch", "turn_on")
    reporter.eq("the same request gets the same id", first, second)
    reporter.check("a different time gets a different id", first != other, (first, other))
    reporter.check("ids carry the bridge prefix", first.startswith("xiaoai_bridge_"), first)
    reporter.check("ids are slugged", all(ch.isalnum() or ch == "_" for ch in first), first)


def scenario_creation(reporter, ns):
    ha = FakeHA()

    async def run(text="每天早上七点半打开热水器"):
        action = parse(ns, text)
        payload = {"entity_id": action["entity_id"]}
        return await ns["_create_ha_automation"](
            ha, action["domain"], action["service"], payload,
            action["schedule_time"], action["schedule_days"], None)

    automation_id, ok = asyncio.run(run())
    reporter.check("first create succeeds", ok is True, ok)
    reporter.eq("HA got exactly one create", len(ha.created), 1)
    reporter.check("the automation id is stable",
                   automation_id.startswith("xiaoai_bridge_0730"), automation_id)
    reporter.eq("the outcome is verified",
                ns["_automation_outcomes"][automation_id]["status"], "verified")
    config = ha.created[0]["config"]
    reporter.eq("the trigger is a time trigger", config["trigger"][0]["at"], "07:30:00")
    reporter.eq("the action targets the entity",
                config["action"][0]["target"]["entity_id"], "switch.reshui")

    # repeating the same sentence must not create a second task
    automation_id2, ok2 = asyncio.run(run())
    reporter.check("the duplicate still reports success", ok2 is True, ok2)
    reporter.eq("the duplicate did not reach HA", len(ha.created), 1)
    reporter.eq("the duplicate reuses the same id", automation_id2, automation_id)
    reporter.eq("the outcome says it already existed",
                ns["_automation_outcomes"][automation_id2]["status"], "exists")

    # ── one-shot tasks: absolute date trigger, not a daily clock ──
    action = parse(ns, "明天八点打开热水器")
    payload = {"entity_id": action["entity_id"], "once": action["once"],
               "once_date": action["once_date"]}

    async def create_once():
        return await ns["_create_ha_automation"](
            ha, action["domain"], action["service"], payload,
            action["schedule_time"], action["schedule_days"], None)

    once_id, once_ok = asyncio.run(create_once())
    reporter.check("a one-shot task is created", once_ok is True, once_ok)
    reporter.eq("its trigger is a clock time (HA has no date-time trigger)",
                ha.created[-1]["config"]["trigger"][0]["at"], "08:00:00")
    reporter.check("the date guard is what makes it one-shot",
                   action["once_date"] in (
                       (ha.created[-1]["config"].get("condition") or [{}])[0]
                       .get("value_template") or ""),
                   ha.created[-1]["config"].get("condition"))
    reporter.eq("it carries a date guard condition",
                (ha.created[-1]["config"].get("condition") or [{}])[0].get("condition"),
                "template")
    meta = [item for item in ns["_schedule_registry_read"]() if item.get("id") == once_id]
    reporter.check("the registry records the one-shot moment",
                   meta and meta[0].get("once_at") == action["once_date"] + " 08:00"
                   and meta[0].get("once") is True, meta)
    once_id2, once_ok2 = asyncio.run(create_once())
    reporter.eq("the same one-shot request reuses the id", once_id2, once_id)
    reporter.eq("the same one-shot request does not create twice",
                len([item for item in ha.created
                     if item["config"]["trigger"][0]["at"].endswith("08:00:00")]), 1)

    later = parse(ns, "后天八点打开热水器")
    later_payload = {"entity_id": later["entity_id"], "once": later["once"],
                     "once_date": later["once_date"]}

    async def create_later():
        return await ns["_create_ha_automation"](
            ha, later["domain"], later["service"], later_payload,
            later["schedule_time"], later["schedule_days"], None)

    later_id, later_ok = asyncio.run(create_later())
    reporter.check("a different day gets its own one-shot id",
                   later_ok and later_id != once_id, (later_id, once_id))

    async def create_in_the_past():
        ha_past = FakeHA()
        result = await ns["_create_ha_automation"](
            ha_past, "switch", "turn_on", {"entity_id": "switch.reshui"},
            "08:00", "once", None, once=True, once_date="2020-01-01")
        return result, ha_past.created

    (past_id, past_ok), past_created = asyncio.run(create_in_the_past())
    reporter.check("a one-shot in the past is refused",
                   past_ok is False and not past_created, (past_id, past_ok))

    # HA accepted the write but never loaded it -> not a success
    ha2 = FakeHA()
    ha2.create_ok = True

    async def created_but_missing():
        ha2.states = []
        return await ns["_create_ha_automation"](
            ha2, "switch", "turn_on", {"entity_id": "switch.reshui"}, "08:00", "daily", None)

    ha2.reload_ok = True
    ha2.create_automation = _drop_after_create(ha2)
    automation_id3, ok3 = asyncio.run(created_but_missing())
    reporter.check("a submitted but unloaded task is not success", ok3 is False, ok3)
    reporter.eq("the outcome says missing",
                ns["_automation_outcomes"][automation_id3]["status"], "missing")

    # reload failure is a failure too
    ha3 = FakeHA()
    ha3.reload_ok = False

    async def reload_fails():
        return await ns["_create_ha_automation"](
            ha3, "switch", "turn_on", {"entity_id": "switch.reshui"}, "09:00", "daily", None)

    automation_id4, ok4 = asyncio.run(reload_fails())
    reporter.check("a failed reload is a failure", ok4 is False, ok4)
    reporter.eq("the outcome says failed",
                ns["_automation_outcomes"][automation_id4]["status"], "failed")


def _drop_after_create(ha):
    async def create_automation(automation_id, config, session=None):
        ha.created.append({"id": automation_id, "config": config})
        return True
    return create_automation


def scenario_weekdays(reporter, ns):
    ha = FakeHA()

    async def run():
        return await ns["_create_ha_automation"](
            ha, "light", "turn_on", {"entity_id": "light.keting"}, "21:00", "1,3,5", None)

    asyncio.run(run())
    condition = ha.created[0]["config"].get("condition") or []
    reporter.check("weekdays become an HA condition", condition, condition)
    if condition:
        reporter.eq("the weekday list is mon/wed/fri",
                    condition[0]["weekday"], ["mon", "wed", "fri"])



def scenario_listing(reporter, ns):
    """The page must list tasks even when HA has not loaded them yet."""
    import os
    if os.path.exists(REGISTRY_PATH):
        os.remove(REGISTRY_PATH)

    ns["_schedule_registry_add"]("xiaoai_bridge_0730_daily_switch_reshui",
                                 "07:30", [], "switch.reshui", "switch", "turn_on",
                                 "voice", False)
    loaded = [{
        "entity_id": "automation.xiaoai_bridge_0730_daily_switch_reshui",
        "state": "on",
        "attributes": {"id": "xiaoai_bridge_0730_daily_switch_reshui",
                       "friendly_name": "小爱桥接-07:30 switch.reshui",
                       "last_triggered": "2026-11-08T07:30:01"},
    }]
    entries = ns["_schedule_listing"](loaded)
    reporter.eq("a loaded task is listed once", len(entries), 1)
    entry = entries[0]
    reporter.eq("status is active", entry["status"], "active")
    reporter.eq("the source is recorded", entry["source"], "voice")
    reporter.eq("the time is recorded", entry["time"], "07:30")
    reporter.check("a next run is computed", entry["next_run"], entry["next_run"])
    reporter.eq("the last trigger is passed through",
                entry["last_triggered"], "2026-11-08T07:30:01")

    # HA lost it (reload failed / not loaded yet) -> still listed, flagged
    entries = ns["_schedule_listing"]([])
    reporter.eq("a task HA has not loaded is still listed", len(entries), 1)
    reporter.eq("and it is flagged as not loaded", entries[0]["status"], "not_loaded")

    # deleting forgets it
    ns["_schedule_registry_remove"]("xiaoai_bridge_0730_daily_switch_reshui")
    reporter.eq("after delete the registry is empty", ns["_schedule_listing"]([]), [])

    # ── one-shot tasks: done after firing, expired when HA never ran them ──
    ns["_schedule_registry_add"]("xiaoai_bridge_once_done", "08:00", [],
                                 "switch.reshui", "switch", "turn_on", "voice", True,
                                 once_at="2026-11-01 08:00")
    fired_states = [{
        "entity_id": "automation.xiao_ai_qiao_jie_2026_11_01_0800",
        "state": "on",
        "attributes": {"id": "xiaoai_bridge_once_done",
                       "friendly_name": "小爱桥接-2026-11-01 08:00 switch.reshui",
                       "last_triggered": "2026-11-01T08:00:02+08:00"},
    }]
    done_entry = [item for item in ns["_schedule_listing"](fired_states)
                  if item["config_id"] == "xiaoai_bridge_once_done"][0]
    reporter.eq("a fired one-shot is marked done", done_entry["status"], "done")
    reporter.check("a fired one-shot has no next run", done_entry["next_run"] is None,
                   done_entry["next_run"])
    reporter.check("a fired one-shot is flagged expired", done_entry["expired"] is True,
                   done_entry)
    kept = asyncio.run(ns["_schedule_purge_expired"](FakeHA(), None, [done_entry]))
    reporter.eq("a fired one-shot is purged from the listing", kept, [])
    reporter.eq("a fired one-shot is forgotten by the registry",
                [item for item in ns["_schedule_registry_read"]()
                 if item.get("id") == "xiaoai_bridge_once_done"], [])

    ns["_schedule_registry_add"]("xiaoai_bridge_once_missed", "09:00", [],
                                 "switch.reshui", "switch", "turn_on", "voice", True,
                                 once_at="2026-11-01 09:00")
    missed_entry = [item for item in ns["_schedule_listing"]([])
                    if item["config_id"] == "xiaoai_bridge_once_missed"][0]
    reporter.eq("a one-shot HA never ran is marked expired",
                missed_entry["status"], "expired")
    kept = asyncio.run(ns["_schedule_purge_expired"](FakeHA(), None, [missed_entry]))
    reporter.eq("a missed one-shot is kept for inspection", len(kept), 1)
    ns["_schedule_registry_remove"]("xiaoai_bridge_once_missed")

    # next-run maths
    from datetime import datetime
    now = datetime(2026, 11, 9, 6, 0)  # Monday 06:00
    reporter.eq("daily 07:30 fires the same day",
                ns["_schedule_next_run"]("07:30", [], False, now), "2026-11-09 07:30")
    reporter.eq("a time already passed rolls to tomorrow",
                ns["_schedule_next_run"]("05:00", [], False, now), "2026-11-10 05:00")
    reporter.eq("weekday rules skip to the next allowed day",
                ns["_schedule_next_run"]("05:00", ["wed"], False, now), "2026-11-11 05:00")
    reporter.eq("a one-shot next run is its absolute moment",
                ns["_schedule_next_run"]("08:00", [], True, now,
                                         once_at="2026-11-09 08:00"),
                "2026-11-09 08:00")
    reporter.eq("a one-shot in the past has no next run",
                ns["_schedule_next_run"]("08:00", [], True, now,
                                         once_at="2026-11-01 08:00"), None)


def main():
    bridge = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/bridge.py")
    assert bridge.exists(), "built bridge.py not found: %s" % bridge
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ns = build_namespace(bridge)
    reporter = Reporter()
    print("voice schedule tests | %s" % bridge)
    for name, scenario in (("chinese parsing", scenario_parsing),
                           ("merged listing", scenario_listing),
                           ("stable ids", scenario_stable_ids),
                           ("verified creation", scenario_creation),
                           ("weekday conditions", scenario_weekdays)):
        scenario(reporter, ns)
        print("  --  " + name)
    if reporter.failures:
        print("voice schedule tests FAILED (%d checks, %d failures)"
              % (reporter.checks, len(reporter.failures)))
        for item in reporter.failures:
            print("  - " + item)
        return 1
    print("voice schedule tests passed (%d checks)" % reporter.checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
