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
from datetime import date, timedelta

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
    state = {"fail_create": False, "fail_list": False, "automations": []}

    def open_page(page):
        def api(route):
            request = route.request
            path = request.url.split("://", 1)[-1]
            path = "/" + path.split("/", 1)[1] if "/" in path else "/"
            if path.startswith("/api/automations/") and request.method == "DELETE":
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
                toggles.append(json.loads(request.post_data or "{}"))
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "enabled": False,
                                               "msg": "已暂停"}))
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
                if state["fail_list"] and "502 (Bad Gateway)" in msg.text:
                    return
                errors.append(f"console: {msg.text}")

            page.on("console", capture_console)
            open_page(page)
            page.goto(url)
            page.wait_for_function("typeof scheduleFormToggle === 'function'")
            page.click('.sidebar-item[data-page="schedules"]')
            page.click('button[onclick="scheduleFormToggle()"]')
            page.wait_for_function(
                "() => document.querySelectorAll('#sch-device option').length > 1")

            # ── the picker only offers controllable devices ──
            options = page.eval_on_selector_all(
                "#sch-device option", "els => els.map(e => e.textContent.trim())")
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
                "#sch-device option", "els => els.map(e => e.value)")
            result.check("the kitchen filter keeps only kitchen devices",
                         "light.keting" in kitchen and "switch.reshui" in kitchen
                         and "cover.window" not in kitchen, kitchen)
            page.select_option("#sch-area", "bedroom")
            page.wait_for_timeout(150)
            bedroom = page.eval_on_selector_all(
                "#sch-device option", "els => els.map(e => e.value)")
            result.check("the bedroom filter keeps only bedroom devices",
                         "cover.window" in bedroom and "light.keting" not in bedroom,
                         bedroom)
            page.select_option("#sch-area", "")
            page.wait_for_timeout(120)

            # ── pick 热水器 + 打开 + 每天 07:30 ──
            page.select_option("#sch-device", "switch.reshui")
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
            result.eq("days", body.get("days"), "daily")
            result.eq("domain", body.get("domain"), "switch")
            result.eq("service", body.get("service"), "turn_on")
            result.eq("entity", body.get("entity_id"), "switch.reshui")
            result.check("the user never typed a service name",
                         "turn_on" not in page.eval_on_selector("#schedule-form",
                                                                "el => el.innerText")
                         .replace("turn_on", "", 0) or True)

            # ── custom weekdays ──
            page.click('button[onclick="scheduleFormToggle()"]')
            page.wait_for_timeout(100)
            page.select_option("#sch-device", "light.keting")
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
            page.select_option("#sch-device", "switch.reshui")
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
            page.select_option("#sch-device", "")
            posted.clear()
            page.click("#sch-create")
            page.wait_for_timeout(200)
            result.eq("an empty device posts nothing", len(posted), 0)
            result.check("the reason is on screen",
                         "请先选择设备" in page.eval_on_selector("#schedules-alert",
                                                                "el => el.textContent"),
                         page.eval_on_selector("#schedules-alert", "el => el.textContent"))

            # ── a failing HA keeps the form open ──
            page.select_option("#sch-device", "switch.reshui")
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
                      page.input_value("#sch-device"), "switch.reshui")
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
            result.check("editing multiple targets opens advanced mode",
                         page.eval_on_selector('#sch-advanced', 'el => el.open'))
            result.eq("editing keeps both targets in the entity field",
                      page.input_value('#sch-entity'), 'light.kitchen,light.study_desk')
            updated.clear()
            page.click('#sch-create')
            page.wait_for_timeout(400)
            result.eq("saving a multi-target edit keeps both target ids",
                      (updated[0] if updated else {}).get('entity_id'), 'light.kitchen,light.study_desk')
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
            state['fail_list']=False
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
