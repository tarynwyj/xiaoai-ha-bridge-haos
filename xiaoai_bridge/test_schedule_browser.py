#!/usr/bin/env python3
"""Real-browser regression for the 定时任务 form (Playwright + Chromium).

Drives the built page with a fake Home Assistant: /api/devices returns real
entities, /api/automations records what the page posted. No HA instance and no
Xiaomi account are involved.

    python xiaoai_bridge/test_schedule_browser.py --html <built web/index.html>
    python xiaoai_bridge/test_schedule_browser.py --upstream _local/02-upstream/pinned-e4c82d4

Covers the "普通用户不写代码也能建任务" path: pick a device, pick a Chinese
action, pick a time and a repeat rule, preview it, create it, and confirm the
POST body contains the right domain/service/entity_id.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from test_scope_browser import Result, build_page, serve  # noqa: E402

DEVICES = [
    {"entity_id": "light.keting", "name": "客厅主灯", "domain": "light", "state": "on"},
    {"entity_id": "switch.reshui", "name": "热水器", "domain": "switch", "state": "off"},
    {"entity_id": "cover.window", "name": "卧室窗帘", "domain": "cover", "state": "open"},
    {"entity_id": "media_player.tv", "name": "客厅电视", "domain": "media_player", "state": "idle"},
    {"entity_id": "sensor.temp", "name": "温度传感器", "domain": "sensor", "state": "23"},
]

AREAS = [
    {"id": "kitchen", "name": "厨房", "entities": ["light.keting", "switch.reshui"]},
    {"id": "bedroom", "name": "卧室", "entities": ["cover.window", "media_player.tv"]},
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--html", help="already-built web/index.html")
    ap.add_argument("--upstream", help="pinned upstream directory")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="schedule-browser-"))
    if args.html:
        html = pathlib.Path(args.html).resolve()
    elif args.upstream:
        html = build_page(pathlib.Path(args.upstream).resolve(), tmp / "build")
    else:
        raise SystemExit("pass --html <built index.html> or --upstream <pinned dir>")

    httpd, url = serve(html.parent)
    result = Result()
    result.eq("built page contains one active schedule list function",
              html.read_text(encoding="utf-8").count("async function loadSchedules("), 1)
    errors: list[str] = []
    posted: list[dict] = []
    toggles: list[dict] = []
    updated: list[dict] = []
    cleanups: list[str] = []
    deleted: list[str] = []
    toggle_ids: list[str] = []
    state = {"fail_create": False, "fail_list": False, "automations": []}

    def open_page(page):
        def api(route):
            request = route.request
            path = urlsplit(request.url).path
            if path.startswith("/api/automations/") and request.method == "DELETE":
                config_id = unquote(path.rsplit("/", 1)[-1])
                deleted.append(config_id)
                if config_id != "xiaoai_bridge_partial":
                    state["automations"] = [s for s in state["automations"] if s["config_id"] != config_id]
                    route.fulfill(json={"ok": True, "ha_deleted": True, "registry_updated": True, "reloaded": True})
                    return
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "ok": True, "ha_deleted": True, "registry_updated": False, "reloaded": True,
                    "msg": "HA 任务配置已删除，但本地登记清理失败，原登记表已保留"}))
                return
            if path == "/api/automations/cleanup":
                cleanups.append(request.method)
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "removed": 1}))
                return
            if path.startswith("/api/automations/") and path.endswith("/toggle"):
                body = request.post_data_json
                toggles.append(body)
                config_id = unquote(path.split("/")[-2])
                toggle_ids.append(config_id)
                for item in state["automations"]:
                    if item["config_id"] == config_id:
                        item.update(state="on" if body["enabled"] else "off",
                                    status="active" if body["enabled"] else "disabled")
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "enabled": body["enabled"],
                                               "msg": "已启用" if body["enabled"] else "已暂停"}))
                return
            if path.startswith("/api/automations/") and request.method == "PUT":
                body = json.loads(request.post_data or "{}")
                body["__id"] = path.rsplit("/", 1)[-1]
                updated.append(body)
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "reloaded": True,
                                               "automation_id": body["__id"],
                                               "msg": "已保存修改 " + str(body.get("time", ""))}))
                return
            if path.startswith("/api/ha/areas"):
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "supported": True, "areas": AREAS}))
                return
            if path.startswith("/api/automations"):
                if request.method == "POST":
                    posted.append(json.loads(request.post_data or "{}"))
                    if state["fail_create"]:
                        route.fulfill(status=500, content_type="application/json",
                                      body=json.dumps({"ok": False, "detail": "HA 拒绝"}))
                        return
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps({"ok": True, "msg": "已创建定时任务",
                                                   "reloaded": True,
                                                   "automation_id": "xiaoai_bridge_0730_light"}))
                    return
                if state["fail_list"]:
                    if state["fail_list"] in ("plain", "html"):
                        status = 500 if state["fail_list"] == "plain" else 502
                        route.fulfill(status=status, content_type="text/plain" if status == 500 else "text/html",
                                      body="Internal Server Error" if status == 500 else "<html>Bad Gateway</html>")
                        return
                    route.fulfill(status=502, content_type="application/json",
                                  body=json.dumps({"detail": "无法读取 HA 自动化状态，任务状态未知"}))
                    return
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True,
                                               "count": len(state["automations"]),
                                               "automations": state["automations"]}))
                return
            if path.startswith("/api/devices"):
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "count": len(DEVICES),
                                               "devices": DEVICES}))
                return
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True}))

        page.route("**/api/**", api)

    def choose_devices(page, *ids):
        page.locator('button[onclick="scheduleClearDevices()"]').click()
        for entity in ids:
            page.locator('#sch-device input[value="' + entity + '"]').check()

    def preview(page):
        return page.eval_on_selector("#sch-preview", "el => el.textContent")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
            def capture_console(msg):
                if msg.type != "error":
                    return
                if state["fail_create"] and "500 (Internal Server Error)" in msg.text:
                    return
                if state["fail_list"] and ("502 (Bad Gateway)" in msg.text or "500 (Internal Server Error)" in msg.text):
                    return
                errors.append(f"console: {msg.text}")

            page.on("console", capture_console)
            open_page(page)
            page.goto(url)
            page.wait_for_function("typeof scheduleFormToggle === 'function'")
            page.click('.sidebar-item[data-page="schedules"]')
            page.click('button[onclick="scheduleFormToggle()"]')
            page.wait_for_function(
                "() => document.querySelectorAll('#sch-device input').length > 1")

            # ── the picker only offers controllable devices ──
            options = page.eval_on_selector_all(
                '#sch-device label', 'els => els.map(e => e.textContent.trim())')
            result.check("the sensor is not offered",
                         not any("温度" in o for o in options), options)
            result.check("the light is offered", any("客厅主灯" in o for o in options), options)
            result.check("the water heater is offered", any("热水器" in o for o in options), options)

            # ── room filter ──
            areas = page.eval_on_selector_all(
                "#sch-area option", "els => els.map(e => e.textContent.trim())")
            result.check("the room filter is populated",
                         any("厨房" in a for a in areas), areas)
            page.select_option("#sch-area", "kitchen")
            page.wait_for_timeout(150)
            kitchen = page.eval_on_selector_all(
                "#sch-device input", "els => els.map(e => e.value)")
            result.check("the kitchen filter keeps only kitchen devices",
                         "light.keting" in kitchen and "switch.reshui" in kitchen
                         and "cover.window" not in kitchen, kitchen)
            page.select_option("#sch-area", "bedroom")
            page.wait_for_timeout(150)
            bedroom = page.eval_on_selector_all(
                "#sch-device input", "els => els.map(e => e.value)")
            result.check("the bedroom filter keeps only bedroom devices",
                         "cover.window" in bedroom and "light.keting" not in bedroom,
                         bedroom)
            page.select_option("#sch-area", "")
            page.wait_for_timeout(120)

            # Selection survives filters, and incompatible targets cannot post.
            choose_devices(page, "switch.reshui")
            page.select_option("#sch-area", "bedroom")
            page.locator('#sch-device input[value="cover.window"]').check()
            result.eq("cross-room selection keeps both targets", page.evaluate('scheduleDeviceIds'),
                      ['switch.reshui', 'cover.window'])
            posted.clear()
            page.click('#sch-create')
            result.eq("incompatible device actions never post a task", len(posted), 0)
            result.check("incompatible actions explain how to split the task", '共同操作' in page.inner_text('#schedules-alert'))
            page.select_option("#sch-area", "")
            choose_devices(page, "light.keting", "switch.reshui")
            page.fill('#sch-search', '窗帘')
            result.eq("search never drops hidden selected devices", page.evaluate('scheduleDeviceIds'),
                      ['light.keting', 'switch.reshui'])
            page.fill('#sch-search', '')
            result.eq("selected checkboxes return after clearing search", page.locator('#sch-device input:checked').count(), 2)
            page.evaluate("haServiceCache.light=['turn_on'];scheduleRenderActions()")
            result.eq("mixed actions respect HA's available service catalogue",
                      page.locator('input[name=sch-action]').evaluate_all('els => els.map(e => e.value)'), ['turn_on'])
            page.evaluate("haServiceCache={};scheduleRenderActions()")
            page.click('button[onclick="scheduleClearDevices()"]')
            page.select_option('#sch-area', 'kitchen')
            page.click('button[onclick="scheduleSelectVisible()"]')
            result.eq("select visible respects the room filter", page.evaluate('scheduleDeviceIds'),
                      ['light.keting', 'switch.reshui'])
            page.select_option('#sch-area', '')

            # ── pick 热水器 + 打开 + 每天 07:30 ──
            choose_devices(page, "switch.reshui")
            page.wait_for_timeout(80)
            actions = page.eval_on_selector_all(
                "input[name=sch-action]", "els => els.map(e => e.value)")
            result.eq("a switch offers on/off/toggle", actions,
                      ["turn_on", "turn_off", "toggle"])
            page.click("input[name=sch-action][value=turn_on]")
            page.fill("#sch-time", "07:30")
            page.dispatch_event("#sch-time", "change")
            page.select_option("#sch-days", "daily")
            page.wait_for_timeout(80)
            result.check("the preview reads like a sentence",
                         "每天" in preview(page) and "07:30" in preview(page)
                         and "打开" in preview(page) and "热水器" in preview(page),
                         preview(page))

            # ── create without typing any code ──
            posted.clear()
            page.click("#sch-create")
            page.wait_for_timeout(300)
            result.eq("exactly one create request", len(posted), 1)
            body = posted[0] if posted else {}
            result.eq("time", body.get("time"), "07:30")
            result.eq("existing time form posts a point task", body.get("kind"), "point")
            result.eq("days", body.get("days"), "daily")
            result.eq("domain", body.get("domain"), "switch")
            result.eq("service", body.get("service"), "turn_on")
            result.eq("entity", body.get("entity_id"), "switch.reshui")


            # ── multiple compatible devices create one array-target task ──
            page.click('button[onclick="scheduleFormToggle()"]')
            choose_devices(page, 'light.keting', 'switch.reshui')
            page.fill('#sch-time', '07:45')
            page.dispatch_event('#sch-time', 'change')
            page.select_option('#sch-days', 'daily')
            result.check("multi-device preview names both devices",
                         '客厅主灯' in preview(page) and '热水器' in preview(page))
            posted.clear()
            page.click('#sch-create')
            page.wait_for_timeout(300)
            result.eq("multi-device creation sends one task", len(posted), 1)
            result.eq("multi-device creation preserves a target array", (posted[0] if posted else {}).get('entity_id'),
                      ['light.keting', 'switch.reshui'])
            result.eq("mixed compatible domains use the HA generic service", (posted[0] if posted else {}).get('domain'), 'homeassistant')

            # ── custom weekdays ──
            page.click('button[onclick="scheduleFormToggle()"]')
            page.wait_for_timeout(100)
            choose_devices(page, "light.keting")
            page.wait_for_timeout(80)
            page.select_option("#sch-days", "custom")
            page.wait_for_timeout(80)
            page.click("#sch-weekdays input[value='1']")
            page.click("#sch-weekdays input[value='3']")
            page.click("#sch-weekdays input[value='5']")
            page.wait_for_timeout(80)
            result.check("the preview names the weekdays",
                         "每周一、三、五" in preview(page), preview(page))
            posted.clear()
            page.click("#sch-create")
            page.wait_for_timeout(300)
            result.eq("custom days are posted as 1,3,5",
                      (posted[0] if posted else {}).get("days"), "1,3,5")

            # ── one-shot task: date field + once flag ──
            page.click('button[onclick="scheduleFormToggle()"]')
            page.wait_for_timeout(150)
            choose_devices(page, "switch.reshui")
            page.wait_for_timeout(100)
            page.select_option("#sch-days", "once")
            page.wait_for_timeout(100)
            result.check("the date field appears for a one-shot",
                         page.is_visible("#sch-date"))
            future = (date.today() + timedelta(days=1)).isoformat()
            page.fill("#sch-date", future)
            page.fill("#sch-time", "08:00")
            page.dispatch_event("#sch-time", "change")
            page.wait_for_timeout(100)
            result.check("the preview shows the chosen date",
                         future in preview(page), preview(page))
            posted.clear()
            page.click("#sch-create")
            page.wait_for_timeout(300)
            body = posted[0] if posted else {}
            result.eq("a one-shot posts once=true", body.get("once"), True)
            result.eq("a one-shot posts its date", body.get("date"), future)
            result.eq("a one-shot still posts a valid days value",
                      body.get("days"), "daily")

            # ── no device: refuse locally, never post ──
            page.click('button[onclick="scheduleFormToggle()"]')
            page.wait_for_timeout(100)
            choose_devices(page)
            posted.clear()
            page.click("#sch-create")
            page.wait_for_timeout(200)
            result.eq("an empty device posts nothing", len(posted), 0)
            result.check("the reason is on screen",
                         "请先选择设备" in page.eval_on_selector("#schedules-alert",
                                                                "el => el.textContent"),
                         page.eval_on_selector("#schedules-alert", "el => el.textContent"))

            # ── a failing HA keeps the form open ──
            choose_devices(page, "switch.reshui")
            page.wait_for_timeout(80)
            state["fail_create"] = True
            posted.clear()
            page.click("#sch-create")
            page.wait_for_timeout(300)
            result.eq("the failing create was attempted", len(posted), 1)
            result.check("the form stays open after a failure",
                         page.is_visible("#schedule-form"))
            state["fail_create"] = False

            # ── the list shows next run / source / HA-not-loaded, and can pause ──
            state["automations"] = [
                {"config_id": "xiaoai_bridge_0730_switch_reshui",
                 "entity_id": "automation.xiaoai_bridge_0730_switch_reshui",
                 "alias": "小爱桥接-07:30 switch.reshui", "state": "on",
                 "last_triggered": "2026-11-08T07:30:01", "time": "07:30",
                 "weekdays": [], "entity": "switch.reshui", "domain": "switch",
                 "service": "turn_on", "data": {}, "once": False, "once_at": "",
                 "source": "voice", "next_run": "2026-11-09 07:30",
                 "status": "active"},
                {"config_id": "xiaoai_bridge_0900_daily_light_a_light_turn_off",
                 "entity_id": "", "alias": "小爱桥接-09:00 light.a（HA 未加载）",
                 "state": "", "last_triggered": None, "time": "09:00",
                 "weekdays": [], "entity": "light.keting", "domain": "light",
                 "service": "turn_off", "data": {}, "once": False, "once_at": "",
                 "source": "form", "next_run": "2026-11-09 09:00",
                 "status": "not_loaded"},
            ]
            page.click('button[onclick="loadSchedules()"]')
            page.wait_for_timeout(250)
            rows = page.eval_on_selector_all(
                "#schedule-list .rule-item", "els => els.map(e => e.textContent)")
            result.eq("both tasks are listed", len(rows), 2)
            joined = " | ".join(rows)
            result.check("the row shows the next run", "下次: 2026-11-09 07:30" in joined, joined)
            result.check("the row shows the source", "来源: 语音" in joined, joined)
            result.check("the row shows the last trigger", "上次:" in joined, joined)
            result.check("the task HA has not loaded is flagged", "HA 未加载" in joined, joined)
            # The page periodically replaces list rows. Locators re-resolve
            # after a refresh; cached ElementHandles can detach before click.
            pause = page.locator(
                '#schedule-list button[onclick^="toggleSchedule"]')
            result.eq("only the loaded task offers pause", pause.count(), 1)
            toggles.clear()
            if pause.count():
                pause.first.click()
                page.wait_for_timeout(300)
            result.eq("pausing posts once", len(toggles), 1)
            result.eq("pausing asks HA to disable it",
                      (toggles[0] if toggles else {}).get("enabled"), False)

            # ── edit: prefilled form, PUT to the same config id ──
            edit_buttons = page.locator(
                '#schedule-list button[onclick^="editSchedule"]')
            result.eq("loaded tasks offer edit", edit_buttons.count(), 2)
            if edit_buttons.count():
                page.evaluate('loadSchedules()')  # force the row replacement race
                edit_buttons.first.click()
                page.wait_for_timeout(600)
            result.check("the edit form is open", page.is_visible("#schedule-form"))
            result.check("the edit banner names the task",
                         "小爱桥接-07:30" in page.inner_text("#sch-edit-banner"),
                         page.inner_text("#sch-edit-banner"))
            result.eq("the time is prefilled", page.input_value("#sch-time"), "07:30")
            result.eq("the device is prefilled",
                      page.locator('#sch-device input:checked').get_attribute('value'), 'switch.reshui')
            page.fill("#sch-time", "08:45")
            page.dispatch_event("#sch-time", "change")
            posted.clear()
            updated.clear()
            page.click("#sch-create")
            page.wait_for_timeout(400)
            result.eq("editing sends one PUT", len(updated), 1)
            result.eq("editing sends no POST", len(posted), 0)
            body = updated[0] if updated else {}
            result.eq("the PUT carries the new time", body.get("time"), "08:45")
            result.eq("the PUT targets the same config id", body.get("__id"),
                      "xiaoai_bridge_0730_switch_reshui")
            result.eq("the PUT keeps the service", body.get("service"), "turn_on")

            page.evaluate("""() => {
                scheduleItems=[{config_id:'xiaoai_bridge_multi',entity:['light.kitchen','light.study_desk'],
                  domain:'light',service:'turn_on',time:'08:00',weekdays:[],data:{brightness_pct:35}}];
                editSchedule('xiaoai_bridge_multi');
            }""")
            page.wait_for_timeout(400)
            result.check("editing multiple targets stays in the checkbox form",
                         not page.eval_on_selector('#sch-advanced', 'el => el.open'))
            result.eq("editing keeps both targets in the entity field",
                      page.input_value('#sch-entity'), 'light.kitchen,light.study_desk')
            result.eq("editing shows both retained targets as checked", page.locator('#sch-device input:checked').count(), 2)
            updated.clear()
            page.click('#sch-create')
            page.wait_for_timeout(400)
            result.eq("saving a multi-target edit keeps both target ids",
                      (updated[0] if updated else {}).get('entity_id'), ['light.kitchen', 'light.study_desk'])
            result.eq("reading the list never calls cleanup", cleanups, [])
            page.locator('button[onclick="cleanupSchedules()"]').click()
            page.wait_for_timeout(400)
            result.eq("explicit cleanup sends POST exactly once", cleanups, ['POST'])
            page.evaluate("""() => {
                scheduleItems=[{config_id:'xiaoai_bridge_seconds',entity:'light.kitchen',
                  domain:'light',service:'turn_on',time:'10:01:10',weekdays:[],data:{}}];
                editSchedule('xiaoai_bridge_seconds');
            }""")
            page.wait_for_timeout(400)
            result.eq("editing retains seconds in the time input", page.input_value('#sch-time'), '10:01:10')
            updated.clear()
            page.click('#sch-create')
            page.wait_for_timeout(400)
            result.eq("saving retains seconds in the API payload",
                      (updated[0] if updated else {}).get('time'), '10:01:10')
            page.on('dialog', lambda dialog: dialog.accept())
            page.evaluate("deleteSchedule('xiaoai_bridge_partial')")
            page.wait_for_timeout(400)
            alert = page.inner_text('#schedules-alert')
            result.check("partial deletion tells the user HA config was deleted", '已删除' in alert)
            result.check("partial deletion explains the remaining registry failure", '登记清理失败' in alert)
            result.check("partial deletion is a warning", 'warn' in page.locator('#schedules-alert').get_attribute('class'))
            result.check("partial deletion never claims total failure", '删除失败' not in alert and '保存' not in alert)
            state['automations'] = [{'config_id': 'xiaoai_bridge_uncertain', 'entity': 'light.kitchen',
                'domain': 'light', 'service': 'turn_on', 'time': '10:01:10', 'weekdays': [],
                'once': True, 'status': 'unconfirmed', 'expired': True, 'loaded': True}]
            page.evaluate('loadSchedules()')
            result.check("near-boundary execution is shown as awaiting confirmation",
                         '待核对' in page.inner_text('#schedule-list'))
            result.check("near-boundary execution is never labelled missed",
                         '已过期未执行' not in page.inner_text('#schedule-list'))
            result.eq("unconfirmed task has no pause control",
                      page.locator('#schedule-list button[onclick^="toggleSchedule("]').count(), 0)
            state['fail_list']=True
            page.evaluate('loadSchedules()')
            page.wait_for_timeout(400)
            result.check("HA outage is shown as unknown, not not-loaded",
                         '状态未知' in page.inner_text('#schedules-alert'))
            result.check("HA outage preserves the API detail message",
                         '无法读取 HA 自动化状态' in page.inner_text('#schedules-alert'))
            result.check("HA outage never shows undefined",
                         'undefined' not in page.inner_text('#schedules-alert'))
            result.eq("HA outage clears misleading cached task rows",
                      page.inner_text('#schedule-count'), '状态未知')
            result.check("HA outage leaves an explicit retry message",
                         '暂时未知' in page.inner_text('#schedule-list'))
            for failure, status in (("plain", 500), ("html", 502)):
                state['fail_list'] = failure
                page.evaluate('loadSchedules()')
                message = page.inner_text('#schedules-alert')
                result.check("non-JSON error includes HTTP status %s" % status, 'HTTP '+str(status) in message)
                result.check("non-JSON error names the recovery source", '加载项日志' in message)
                result.check("non-JSON error never exposes a parser exception",
                             'Unexpected token' not in message and 'undefined' not in message)
            state['fail_list']=False
            # Freeze just before the deadline; navigation and form actions must
            # not consume any of the interval that the test explicitly advances.
            clock_start = datetime(2025, 12, 1, 8, tzinfo=timezone.utc)
            page.clock.install(time=clock_start - timedelta(minutes=1))
            page.clock.pause_at(clock_start)
            state['automations'] = [
                {'config_id': 'group/window', 'entity_id': 'automation.window', 'alias': '窗时段',
                 'kind': 'window', 'entity': ['light.keting', 'switch.reshui'], 'domain': 'homeassistant',
                 'service': 'turn_on', 'time': '22:00:15', 'end_time': '06:00:30', 'weekdays': ['fri'],
                 'state': 'on', 'status': 'active'},
                {'config_id': 'countdown/active', 'entity_id': 'automation.countdown_active', 'alias': '正在倒计时',
                 'kind': 'countdown', 'entity': 'switch.reshui', 'domain': 'switch', 'service': 'turn_off',
                 'duration_minutes': 30, 'deadline': '2025-12-01T16:00:05+08:00',
                 'once': True, 'state': 'on', 'status': 'active'},
                {'config_id': 'countdown/done', 'entity_id': 'automation.countdown_done', 'alias': '历史完成',
                 'kind': 'countdown', 'entity': 'switch.reshui', 'status': 'done', 'once': True},
                {'config_id': 'countdown/expired', 'entity_id': 'automation.countdown_expired', 'alias': '历史错过',
                 'kind': 'countdown', 'entity': 'switch.reshui', 'status': 'expired', 'once': True},
                {'config_id': 'unknown-target', 'entity_id': 'automation.unknown', 'alias': '未发现设备任务',
                 'entity': 'switch.old', 'state': 'off', 'status': 'disabled'},
            ]
            page.locator('[data-page="devices"]').click()
            sensor = page.locator('#device-list .rule-item').filter(has_text='温度传感器')
            sensor.wait_for()
            result.eq('read-only device has no schedule entry', sensor.get_by_role('button', name='定时').count(), 0)
            page.locator('#device-list [data-entity="switch.reshui"]').click()
            rows = page.locator('#schedule-list .rule-item')
            rows.filter(has_text='正在倒计时').wait_for()
            result.eq('device entry scopes the schedule list', page.input_value('#schedule-device-filter'), 'switch.reshui')
            result.eq('device scope matches both scalar and array targets', rows.count(), 4)
            result.check('a scoped refresh clears the previous list error', not page.is_visible('#schedules-alert'))
            page.locator('button[onclick="loadSchedules()"]').click()
            page.wait_for_function('document.querySelector("#schedule-count").textContent === "4个"')
            result.eq('refresh preserves device selection', page.input_value('#schedule-device-filter'), 'switch.reshui')
            page.select_option('#schedule-device-filter', 'switch.old')
            result.eq('a retained unavailable target remains filterable', rows.count(), 1)
            page.select_option('#schedule-device-filter', '')
            result.eq('all devices restores all logical tasks', rows.count(), 5)
            page.select_option('#schedule-device-filter', 'switch.reshui')

            window = rows.filter(has_text='窗时段')
            countdown = rows.filter(has_text='正在倒计时')
            result.check('one grouped window shows repeat and next-day close',
                         '每周五' in window.inner_text() and '次日 06:00:30' in window.inner_text())
            result.check('completed and missed countdowns use backend status',
                         '已完成' in rows.filter(has_text='历史完成').inner_text()
                         and '已过期未执行' in rows.filter(has_text='历史错过').inner_text())
            remaining = countdown.locator('[data-deadline]')
            result.eq('offset deadline has five seconds remaining', remaining.inner_text(), '剩余 00:00:05')
            page.clock.run_for(2000)
            result.eq('countdown ticks in place', remaining.inner_text(), '剩余 00:00:03')
            page.clock.run_for(5000)
            result.check('local zero does not claim execution', '刷新核对' in remaining.inner_text())
            page.locator('[data-page="devices"]').click()
            result.check('leaving the page clears the countdown timer', page.evaluate('scheduleClock === null'))
            page.locator('[data-page="schedules"]').click()

            with page.expect_response(lambda r: r.url.endswith('/toggle')):
                window.get_by_title('暂停/启用').click()
            window.get_by_text('暂停', exact=True).wait_for()
            result.eq('pause uses the group config id and one request', (toggle_ids[-1], toggles[-1]),
                      ('group/window', {'enabled': False}))
            with page.expect_response(lambda r: r.url.endswith('/toggle')):
                window.get_by_title('暂停/启用').click()
            window.get_by_text('启用', exact=True).wait_for()
            result.eq('resume uses the same group id', (toggle_ids[-1], toggles[-1]),
                      ('group/window', {'enabled': True}))

            window.get_by_title('修改任务').click()
            create = page.locator('#sch-create')
            page.wait_for_function('!document.querySelector("#sch-create").disabled')
            result.eq('edit loads the grouped window type and both clocks',
                      [page.input_value(sel) for sel in ('#sch-kind', '#sch-time', '#sch-end-time')],
                      ['window', '22:00:15', '06:00:30'])
            result.eq('edit retains custom weekdays and both targets',
                      (page.locator('#sch-weekdays :checked').get_attribute('value'), page.locator('#sch-device :checked').count()), ('5', 2))
            page.fill('#sch-end-time', '07:00')
            with page.expect_response(lambda r: r.request.method == 'PUT'):
                create.click()
            page.locator('#schedule-form').wait_for(state='hidden')
            result.eq('group edit sends both clocks in one PUT', updated[-1],
                      {'__id': 'group%2Fwindow', 'kind': 'window', 'time': '22:00:15', 'end_time': '07:00',
                       'days': '5', 'entity_id': ['light.keting', 'switch.reshui'], 'domain': 'homeassistant', 'service': 'turn_on'})

            page.locator('#page-schedules button[onclick="scheduleFormToggle()"]:visible').first.click()
            page.wait_for_function('!document.querySelector("#sch-create").disabled')
            result.eq('new form starts clean with only the context device',
                      (page.input_value('#sch-kind'), page.locator('#sch-device :checked').get_attribute('value')), ('point', 'switch.reshui'))
            page.select_option('#sch-kind', 'window')
            result.check('window hides advanced mode and disables one-shot',
                         not page.is_visible('#sch-advanced') and page.locator('#sch-days option[value=once]').is_disabled())
            page.fill('#sch-time', '22:00')
            page.fill('#sch-end-time', '22:00')
            start_posts = len(posted)
            create.click()
            result.check('equal clocks are rejected locally', '不能相同' in page.inner_text('#sch-status'))
            choose_devices(page, 'cover.window')
            create.click()
            result.check('incompatible window targets are rejected locally', '同时支持打开和关闭' in page.inner_text('#sch-status'))
            result.eq('invalid windows make no POST', len(posted), start_posts)
            choose_devices(page, 'switch.reshui')
            page.fill('#sch-end-time', '06:00')
            page.select_option('#sch-days', 'weekdays')
            result.check('window preview identifies start-day repeat and next-day close',
                         '周一至周五' in preview(page) and '次日 06:00' in preview(page))
            with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/api/automations')):
                create.click()
            page.locator('#schedule-form').wait_for(state='hidden')
            result.eq('window is posted as a single logical task', posted[-1],
                      {'kind': 'window', 'time': '22:00', 'end_time': '06:00', 'days': 'weekdays',
                       'entity_id': 'switch.reshui', 'domain': 'switch', 'service': 'turn_on'})

            countdown.get_by_title('修改任务').click()
            page.wait_for_function('!document.querySelector("#sch-create").disabled')
            result.check('countdown editing explicitly requires a new duration',
                         page.is_visible('#sch-countdown-edit') and page.input_value('#sch-duration') == '')
            result.eq('countdown edit preserves the selected action',
                      page.locator('input[name=sch-action]:checked').get_attribute('value'), 'turn_off')
            create.click()
            result.check('an empty edit duration cannot restart accidentally', '整数分钟' in page.inner_text('#sch-status'))
            page.fill('#sch-duration', '12')
            with page.expect_response(lambda r: r.request.method == 'PUT'):
                create.click()
            page.locator('#schedule-form').wait_for(state='hidden')
            result.eq('restarting uses numeric minutes and the existing id, with no wall clock', updated[-1],
                      {'__id': 'countdown%2Factive', 'kind': 'countdown', 'duration_minutes': 12,
                       'entity_id': 'switch.reshui', 'domain': 'switch', 'service': 'turn_off', 'data': {}})

            page.locator('#page-schedules button[onclick="scheduleFormToggle()"]:visible').first.click()
            page.wait_for_function('!document.querySelector("#sch-create").disabled')
            page.select_option('#sch-days', 'once')
            page.select_option('#sch-kind', 'countdown')
            result.check('countdown hides calendar fields including a previously selected date',
                         not page.is_visible('#sch-time') and not page.is_visible('#sch-date'))
            start_posts = len(posted)
            for duration in ('0', '1.5', '10081'):
                page.fill('#sch-duration', duration)
                create.click()
                result.check(f'invalid countdown duration {duration} explains the range', '整数分钟' in page.inner_text('#sch-status'))
            result.eq('invalid countdown durations make no POST', len(posted), start_posts)
            page.fill('#sch-duration', '30')
            page.check('input[name=sch-action][value=turn_off]')
            result.check('countdown preview describes a relative delay', '30 分钟后，关闭' in preview(page))
            with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/api/automations')):
                create.click()
            page.locator('#schedule-form').wait_for(state='hidden')
            result.eq('countdown creation omits stale calendar values', posted[-1],
                      {'kind': 'countdown', 'duration_minutes': 30, 'entity_id': 'switch.reshui',
                       'domain': 'switch', 'service': 'turn_off', 'data': {}})

            result.eq('active countdown has no pause control', countdown.get_by_title('暂停/启用').count(), 0)
            with page.expect_response(lambda r: r.request.method == 'DELETE'):
                countdown.get_by_title('取消倒计时').click()
            countdown.wait_for(state='detached')
            result.eq('cancellation deletes the countdown config id', deleted[-1], 'countdown/active')
            result.check('cancellation confirmation is explicit', '已取消倒计时' in page.inner_text('#schedules-alert'))
            result.check('refresh without an active countdown stops the clock', page.evaluate('scheduleClock === null'))
            with page.expect_response(lambda r: r.request.method == 'DELETE'):
                window.get_by_title('删除开关时段').click()
            window.wait_for(state='detached')
            result.eq('deletion targets the entire grouped window via one config id', deleted[-1], 'group/window')
            result.check("the page logged no error", not errors, "; ".join(errors[:3]))
            browser.close()
    finally:
        httpd.shutdown()

    print()
    if result.failures:
        print("schedule browser tests FAILED (%d checks, %d failures)"
              % (result.checks, len(result.failures)))
        for item in result.failures:
            print("  - " + item)
        return 1
    print("schedule browser tests passed (%d checks)" % result.checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
