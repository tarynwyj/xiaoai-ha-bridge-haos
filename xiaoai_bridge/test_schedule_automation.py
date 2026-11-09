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
    "_schedule_targets",
    "_sync_schedule_clock",
    "_normalize_schedule_time",
    "_normalize_schedule_weekdays",
    "_normalize_schedule_date",
    "_schedule_now",
    "_schedule_once_at",
    "_schedule_form_date",
    "_schedule_local_naive",
    "_schedule_once_state",
    "_schedule_purge_expired",
    "_schedule_automation_entity",
    "_schedule_parse_areas",
    "_schedule_form_days",
    "_schedule_form_action",
    "_schedule_form_data",
    "_automation_entries",
    "_schedule_ha_target_error",
    "_automation_config_state",
    "_create_ha_automation",
    "_automation_stable_id",
    "_automation_outcome",
    "_automation_exists",
    "_schedule_registry_path",
    "_schedule_registry_read",
    "_schedule_registry_write",
    "_schedule_registry_add",
    "_schedule_registry_remove",
    "_schedule_next_run",
    "_schedule_listing",
    "create_automation",
    "update_automation",
    "delete_automation",
    "toggle_automation",
    "ha_services",
    "ha_areas",
)
TABLES = ("_schedule_clock", "_SCHEDULE_WEEKDAY_ORDER", "_SCHEDULE_WEEKDAY_ALIASES",
          "_automation_outcomes",
          "_SCHEDULE_DAY_WORDS", "_SCHEDULE_TARGET_OPTIONAL_DOMAINS",
          "_SCHEDULE_CROSS_DOMAIN_ALLOWED", "_SCHEDULE_RESERVED_DATA_KEYS",
          "_SCHEDULE_INTERNAL_KEYS", "_SCHEDULE_AREAS_TEMPLATE")

CURRENT_CONFIG = {"homeassistant": {"url": "http://ha.local:8123", "token": "ha-token"}}
CREATED_HA = []
ROUTES = {}
SERVICE_CALLS = []
_REGISTRY_PATH = __import__("os").path.join(
    __import__("tempfile").gettempdir(), "xiaoai-test-schedules.json")


class FakeHA:
    create_ok = True
    reload_ok = True
    delete_ok = True
    service_ok = True
    # HA's automations are shared state, not a property of one HTTP client:
    # keep the list on the class so every HAClient() sees the same tasks.
    created_ids = []

    def __init__(self):
        self.created = []
        self.deleted = []
        self.base = "http://ha.local:8123"
        self.headers = {"Authorization": "Bearer ha-token"}

    async def create_automation(self, automation_id, config, session):
        self.created.append(config)
        if type(self).create_ok:
            # A real HA exposes the new automation through /api/states once the
            # config is written and reloaded; mirror that so the bridge's
            # read-back check sees what it would see on a real instance.
            type(self).created_ids.append(automation_id)
        return type(self).create_ok

    async def call_service(self, domain, service, data, session):
        SERVICE_CALLS.append((domain, service, (data or {}).get("entity_id")))
        return type(self).service_ok

    async def reload_automations(self, session):
        return type(self).reload_ok

    async def delete_automation(self, automation_id, session):
        self.deleted.append(automation_id)
        ids = type(self).created_ids
        if automation_id in ids:
            ids.remove(automation_id)
        return type(self).delete_ok

    async def list_automation_states(self, session):
        return [
            {"entity_id": "automation." + item, "state": "on",
             "attributes": {"id": item, "friendly_name": item}}
            for item in type(self).created_ids
        ]


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

    def delete(self, path):
        def decorator(func):
            func.route = path
            return func

        return decorator

    def put(self, path):
        def decorator(func):
            func.route = path
            return func

        return decorator

    def get(self, path):
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

    async def text(self):
        return self._payload if isinstance(self._payload, str) else json.dumps(self._payload)


class StubSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _route(self, url):
        if str(url).endswith("/api/config"):
            return FakeResponse(200, {"time_zone": "UTC"})
        for prefix, (status, payload) in ROUTES.items():
            if str(url).startswith(prefix):
                return FakeResponse(status, payload)
        return FakeResponse(404, None)

    def get(self, url, **kwargs):
        return self._route(url)

    def post(self, url, **kwargs):
        return self._route(url)


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
        "Path": __import__("pathlib").Path,
        "_SCHEDULE_REGISTRY_OVERRIDE": _REGISTRY_PATH,
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
    form_data = ns["_schedule_form_data"]
    automation_entries = ns["_automation_entries"]
    create = ns["_create_ha_automation"]
    create_endpoint = ns["create_automation"]
    delete_endpoint = ns["delete_automation"]
    toggle_endpoint = ns["toggle_automation"]
    services_endpoint = ns["ha_services"]
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

    # 同一个请求必须是幂等的：重复说同一句话只会有一条任务（旧实现用时间戳+随机后缀，
    # 同一秒连说两次会得到两条任务，正是 issue 里「重复语音产生多条任务」的成因）。
    repeated = set()
    for _ in range(25):
        same_request_id, _ok, _created = asyncio.run(run("07:30", None))
        repeated.add(same_request_id)
    check("repeating the same request yields one stable config id",
          len(repeated) == 1, repeated)

    # 不同请求仍然要拿到不同的 config id
    other_time, _ok, _created = asyncio.run(run("07:31", None))
    check("a different request gets a different config id",
          other_time not in repeated, (other_time, repeated))

    # ── 暂停/启用：页面按钮最终落到 HA 的 automation.turn_off / turn_on ──
    ROUTES.clear()
    SERVICE_CALLS.clear()
    # The HA entity id is derived from the alias, not from the config id, so the
    # endpoint has to resolve it from /api/states. Only a loaded task can toggle.
    FakeHA.created_ids.append("xiaoai_bridge_0730_light")

    def run_toggle(automation_id, enabled):
        try:
            return asyncio.run(toggle_endpoint(automation_id, {"enabled": enabled})), None
        except HttpError as exc:
            return None, exc

    payload, error = run_toggle("xiaoai_bridge_0730_light", False)
    check("pausing returns ok", error is None and payload and payload.get("ok"), (payload, error))
    eq("pausing calls automation.turn_off",
       SERVICE_CALLS[-1:], [("automation", "turn_off", "automation.xiaoai_bridge_0730_light")])
    eq("pausing reports 已暂停", (payload or {}).get("msg"), "已暂停")

    payload, error = run_toggle("xiaoai_bridge_0730_light", True)
    check("resuming returns ok", error is None and payload and payload.get("ok"), (payload, error))
    eq("resuming calls automation.turn_on",
       SERVICE_CALLS[-1:], [("automation", "turn_on", "automation.xiaoai_bridge_0730_light")])
    eq("resuming reports 已启用", (payload or {}).get("msg"), "已启用")

    # HA 拒绝时必须报错，不能假装成功
    FakeHA.service_ok = False
    try:
        payload, error = run_toggle("xiaoai_bridge_0730_light", False)
    finally:
        FakeHA.service_ok = True
    check("a refused toggle raises instead of reporting success",
          error is not None and payload is None, (payload, error))

    payload, error = run_toggle("xiaoai_bridge_missing", False)
    check("toggling a task HA never loaded returns 404",
          payload is None and error is not None and error.status_code == 404, error)
    payload, error = run_toggle("automation.foreign", False)
    check("toggling an automation from elsewhere is refused",
          payload is None and error is not None and error.status_code == 403, error)
    FakeHA.created_ids.remove("xiaoai_bridge_0730_light")

    # ── /api/ha/services：表单要能拿到 HA 真正提供的服务 ──
    ROUTES["http://ha.local:8123/api/services"] = (
        200,
        [{"domain": "switch", "services": {"turn_on": {}, "turn_off": {}, "toggle": {}}},
         {"domain": "light", "services": {"turn_on": {}, "turn_off": {}}},
         {"domain": "sensor", "services": {}}],
    )
    try:
        payload = asyncio.run(services_endpoint())
        eq("services are grouped by domain",
           sorted((payload or {}).get("services", {}).keys()), ["light", "sensor", "switch"])
        eq("service names are listed",
           (payload or {}).get("services", {}).get("switch"), ["toggle", "turn_off", "turn_on"])
    except HttpError as exc:
        check("ha services endpoint must not raise", False, str(exc))
    ROUTES.clear()

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

    data, error = form_data(None)
    check("no service data is fine", error == "" and data == {})
    data, error = form_data({"option": "Heat"})
    check("scalar service data kept", data == {"option": "Heat"}, error)
    data, error = form_data({"option": ["a"]})
    check("nested service data refused", data == {} and "只支持" in error)
    data, error = form_data({"Bad-Key": "x"})
    check("bad data key refused", data == {} and "参数名" in error)
    data, error = form_data("nope")
    check("non-object service data refused", data == {} and "JSON 对象" in error)
    data, error = form_data({"entity_id": "switch.plug_1"})
    check("service data cannot retarget entity_id",
          data == {} and "不能写 entity_id" in error, error)
    data, error = form_data({"target": {"entity_id": "switch.plug_1"}})
    check("service data cannot carry a target", data == {} and "不能写 target" in error, error)
    data, error = form_data({"area_id": "kitchen"})
    check("service data cannot carry an area target",
          data == {} and "不能写 area_id" in error, error)

    # ── the schedules-page form: POST /api/automations ──
    def call(req):
        return asyncio.run(create_endpoint(req))

    def form(time_value, days="daily", domain="light", service="turn_on",
             entity="light.a", data=None):
        return types.SimpleNamespace(time=time_value, days=days, domain=domain,
                                     service=service, entity_id=entity, data=data)

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
    FakeHA.created_ids.clear()
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
    expect_error(form("07:30", data={"entity_id": "switch.plug_1"}),
                 "form endpoint refuses a data-supplied target", "不能写 entity_id", 400)

    # audit P2-3: the target is checked against HA's own service/entity lists
    ROUTES["http://ha.local:8123/api/services"] = (
        200, [{"domain": "light", "services": {"turn_on": {}, "turn_off": {}}},
              {"domain": "switch", "services": {"turn_on": {}}},
              {"domain": "homeassistant",
               "services": {"update_entity": {"fields": {"entity_id": {"required": True}}}}},
              {"domain": "select", "services": {
                  "select_option": {"fields": {"option": {"required": True}}}}}])
    expect_error(form("07:30", service="turn_onn", entity="light.a"),
                 "form refuses an unknown HA service", "服务", 400)
    expect_error(form("07:30", entity="light.ghost"),
                 "form refuses an unknown entity", "找不到实体", 400)
    ROUTES["http://ha.local:8123/api/states/light.a"] = (200, {"entity_id": "light.a"})
    response = call(form("07:30", entity="light.a"))
    check("form accepts a service/entity HA knows", response.get("ok") is True, response)

    # audit P2-3: required service parameters are enforced (select.select_option)
    ROUTES["http://ha.local:8123/api/states/select.scene"] = (200, {"entity_id": "select.scene"})
    expect_error(form("07:30", domain="select", service="select_option",
                      entity="select.scene"),
                 "form refuses a service without its required parameter",
                 "还需要参数", 400)
    response = call(form("07:30", domain="select", service="select_option",
                         entity="select.scene", data={"option": "Heat"}))
    check("form passes service data into the action", response.get("ok") is True, response)
    eq("service data lands in the created action",
       CREATED_HA[-1].created[-1]["action"][0].get("data"), {"option": "Heat"})

    # an entity is only "provided" when the dedicated field actually has one
    ROUTES["http://ha.local:8123/api/states/switch.plug_1"] = (200, {"entity_id": "switch.plug_1"})
    expect_error(form("07:30", domain="homeassistant", service="update_entity", entity=""),
                 "an empty target does not satisfy a required entity_id",
                 "还需要参数", 400)
    response = call(form("07:30", domain="homeassistant", service="update_entity",
                         entity="switch.plug_1"))
    check("an explicit entity satisfies the required entity_id",
          response.get("ok") is True, response)

    # audit P2-4: created but reload failed must not be reported as "not created"
    FakeHA.created_ids.clear()
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
    FakeHA.created_ids.clear()
    FakeHA.reload_ok = False
    try:
        expect_error(form("07:30", entity="light.a"),
                     "a failed creation without a saved config returns 500", "创建失败", 500)
    finally:
        FakeHA.reload_ok = True

    # audit P2-4 / P2-B: an unconfirmable result must not be reported as "not created"
    FakeHA.created_ids.clear()
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

    # ── one-shot tasks from the form ──
    from datetime import datetime, timedelta
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    def form_once(date_value, time_value="08:00", entity="switch.reshui"):
        return types.SimpleNamespace(time=time_value, days="daily", domain="switch",
                                     service="turn_on", entity_id=entity, data=None,
                                     once=True, date=date_value)

    FakeHA.created_ids.clear()
    ROUTES.clear()
    response = call(form_once(tomorrow))
    check("a one-shot form task is created", response.get("ok") is True, response)
    check("its message names the date and time",
          response.get("msg") == "已创建一次性任务 " + tomorrow + " 08:00", response.get("msg"))
    created = CREATED_HA[-1].created[-1]
    eq("its trigger is a clock time (HA has no date-time time trigger)",
       created["trigger"][0]["at"], "08:00:00")
    check("the date guard is what makes it one-shot",
          "template" == (created.get("condition") or [{}])[0].get("condition")
          and tomorrow in (created.get("condition") or [{}])[0].get("value_template", ""),
          created.get("condition"))
    meta = [item for item in ns["_schedule_registry_read"]()
            if item.get("id") == response["automation_id"]]
    check("the registry records it as one-shot",
          meta and meta[0].get("once") is True
          and meta[0].get("once_at") == tomorrow + " 08:00", meta)
    eq("the form source is recorded", (meta or [{}])[0].get("source"), "form")
    expect_error(form_once(""), "a one-shot without a date is refused", "需要选择日期", 400)
    expect_error(form_once("2020-01-01"),
                 "a one-shot in the past is refused", "已经过去", 400)
    expect_error(form_once("2026-02-30"),
                 "an impossible calendar date is refused", "需要选择日期", 400)

    # ── editing an existing task (PUT) ──
    update_endpoint = ns["update_automation"]
    FakeHA.created_ids.clear()

    def edit(automation_id, time_value="08:15", days="weekdays", domain="light",
             service="turn_off", entity="light.a", once=False, date_value=None):
        request = types.SimpleNamespace(time=time_value, days=days, domain=domain,
                                        service=service, entity_id=entity, data=None,
                                        once=once, date=date_value)
        return asyncio.run(update_endpoint(automation_id, request))

    base = call(form("07:30", "daily", entity="light.a"))
    base_id = base["automation_id"]
    response = edit(base_id, "08:15", "weekdays")
    check("editing returns ok", response.get("ok") is True, response)
    eq("editing keeps the same config id", response.get("automation_id"), base_id)
    check("editing says it saved", str(response.get("msg", "")).startswith("已保存修改"),
          response.get("msg"))
    edited = CREATED_HA[-1].created[-1]
    eq("the edited trigger uses the new time", edited["trigger"][0]["at"], "08:15:00")
    eq("the edited condition uses the new weekdays",
       edited["condition"][0]["weekday"], ["mon", "tue", "wed", "thu", "fri"])
    eq("the edited action uses the new service",
       edited["action"][0]["service"], "light.turn_off")
    meta = [item for item in ns["_schedule_registry_read"]() if item.get("id") == base_id]
    check("the registry is updated, not duplicated",
          len(meta) == 1 and meta[0].get("time") == "08:15:00"
          and meta[0].get("weekdays") == ["mon", "tue", "wed", "thu", "fri"], meta)

    try:
        edit("automation.foreign")
        check("editing a foreign automation is refused", False, "no error raised")
    except HttpError as exc:
        check("editing a foreign automation is refused", exc.status_code == 403, exc.detail)
    ROUTES.clear()
    try:
        edit(base_id, time_value="25:00")
        check("editing with an invalid time is refused", False, "no error raised")
    except HttpError as exc:
        check("editing with an invalid time is refused", exc.status_code == 400, exc.detail)
    try:
        edit(base_id, days="8")
        check("editing with an invalid weekday is refused", False, "no error raised")
    except HttpError as exc:
        check("editing with an invalid weekday is refused", exc.status_code == 400, exc.detail)

    # ── rooms/areas proxy for the device filter ──
    areas_endpoint = ns["ha_areas"]
    ROUTES["http://ha.local:8123/api/template"] = (
        200, json.dumps([{"id": "kitchen", "name": "厨房",
                          "entities": ["light.a", "switch.kettle"]},
                         {"id": "bathroom", "name": "浴室", "entities": ["light.b"]},
                         {"name": "no id"}, "junk"]))
    payload = asyncio.run(areas_endpoint())
    check("areas are returned", payload.get("ok") is True and payload.get("supported") is True,
          payload)
    eq("area names and entities are parsed", payload.get("areas"),
       [{"id": "kitchen", "name": "厨房", "entities": ["light.a", "switch.kettle"]},
        {"id": "bathroom", "name": "浴室", "entities": ["light.b"]}])
    ROUTES["http://ha.local:8123/api/template"] = (404, None)
    payload = asyncio.run(areas_endpoint())
    check("HA without the template API degrades to an empty filter",
          payload.get("ok") is True and payload.get("supported") is False,
          payload)
    ROUTES["http://ha.local:8123/api/template"] = (500, None)
    try:
        asyncio.run(areas_endpoint())
        check("a broken HA template call is reported", False, "no error raised")
    except HttpError as exc:
        check("a broken HA template call is reported", exc.status_code == 502, exc.detail)
    ROUTES.clear()

    # ── delete: reload result must be reported, not assumed (audit P2-2) ──
    def delete(id_value):
        return asyncio.run(delete_endpoint(id_value))

    def expect_delete_error(id_value, label, hint, status=400):
        try:
            result = delete(id_value)
        except HttpError as exc:
            check(label, exc.status_code == status and hint in str(exc.detail),
                  f"got HTTP {exc.status_code}: {exc.detail}")
            return
        check(label, False, f"expected HTTPException, got {result}")

    FakeHA.reload_ok = True
    FakeHA.delete_ok = True
    response = delete("xiaoai_bridge_delete_me")
    check("delete reports a successful reload",
          response.get("ok") is True and response.get("reloaded") is True, response)
    FakeHA.reload_ok = False
    try:
        response = delete("xiaoai_bridge_delete_me")
        check("delete warns when the reload failed",
              response.get("ok") is True and response.get("reloaded") is False, response)
    finally:
        FakeHA.reload_ok = True
    FakeHA.delete_ok = False
    ROUTES["http://ha.local:8123/api/config/automation/config/"] = (200, {"alias": "x"})
    try:
        expect_delete_error("xiaoai_bridge_delete_me",
                            "a failed delete returns 500", "删除失败", 500)
    finally:
        FakeHA.delete_ok = True
    ROUTES["http://ha.local:8123/api/config/automation/config/"] = (404, None)
    FakeHA.delete_ok = False
    try:
        response = delete("xiaoai_bridge_gone")
        check("a lost delete response with a vanished config counts as deleted",
              response.get("ok") is True and response.get("reloaded") is True, response)
        ROUTES["http://ha.local:8123/api/config/automation/config/"] = (500, None)
        expect_delete_error("xiaoai_bridge_unknown",
                            "an unconfirmable delete returns 504 with a check message",
                            "无法确认", 504)
    finally:
        FakeHA.delete_ok = True
        ROUTES.pop("http://ha.local:8123/api/config/automation/config/", None)
    expect_delete_error("automation.foreign",
                        "delete refuses automations from elsewhere", "只能删除", 403)

    print()
    if failures:
        print(f"schedule automation tests FAILED ({checks} checks, {len(failures)} failed)")
        return 1
    print(f"schedule automation tests passed ({checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
