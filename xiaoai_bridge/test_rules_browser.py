#!/usr/bin/env python3
"""Real-browser regression for the intent-rules page (Playwright + Chromium).

The page under test is the *built* web/index.html: the pinned upstream HTML with
patch_web.py applied. Nothing is stubbed except the HTTP API: Playwright serves the
real page over loopback HTTP, Chromium executes the real inline script, and
`page.route` answers /api/config, /api/devices and /api/aliases with fixture data.

Why this file exists: test_web_patch.py runs the inline script against a hand-written
DOM stub. That stub once returned a plain Array from querySelectorAll, which hid a
shipped bug (NodeList has no .filter/.map) that broke every button except 编辑/收起 in
real browsers. This suite closes that gap by driving a real browser.

Run:
    python xiaoai_bridge/test_rules_browser.py --html <built>/web/index.html
    python xiaoai_bridge/test_rules_browser.py --upstream <pinned dir>   # builds first
Exit code 0 only when every check passes and the browser logged no errors.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import pathlib
import shutil
import socketserver
import subprocess
import sys
import threading

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent

CONFIG = {
    "xiaomi": {"username": "", "password": "", "deviceId": ""},
    "homeassistant": {"url": "", "token": ""},
    "intent_rules": [
        {
            "name": "客厅灯组",
            "pattern": "打开客厅的灯",
            "action": {"domain": "light", "service": "turn_on",
                       "entity_id": ["light.a", "light.b"], "reply": "好的"},
        },
        {
            "pattern": "关掉卧室灯",
            "action": {"domain": "light", "service": "turn_off",
                       "entity_id": "light.c", "reply": "好的"},
        },
        {
            "name": "睡觉模式",
            "pattern": "睡觉模式",
            "action": {"domain": "light", "service": "turn_on",
                       "entity_id": "", "reply": "好的"},
        },
    ],
}

DEVICES = [
    {"entity_id": "light.a", "name": "客厅主灯", "domain": "light", "state": "on"},
    {"entity_id": "light.b", "name": "客厅射灯", "domain": "light", "state": "off"},
    {"entity_id": "light.c", "name": "卧室灯", "domain": "light", "state": "on"},
    {"entity_id": "switch.d", "name": "插座", "domain": "switch", "state": "off"},
]


class Result:
    def __init__(self) -> None:
        self.checks = 0
        self.failures: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks += 1
        if not ok:
            self.failures.append(f"{name}{(' :: ' + detail) if detail else ''}")
            print(f"  FAIL {name}{(' :: ' + detail) if detail else ''}")
        return ok

    def eq(self, name: str, actual, expected) -> bool:
        return self.check(name, actual == expected, f"expected {expected!r}, got {actual!r}")


def build_page(upstream: pathlib.Path, workdir: pathlib.Path) -> pathlib.Path:
    """Copy the pinned upstream and apply both patch scripts (they must apply once)."""
    index = workdir / "web" / "index.html"
    index.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(upstream / "bridge.py", workdir / "bridge.py")
    shutil.copy2(upstream / "web" / "index.html", index)
    for script, target in (("patch_auth.py", workdir / "bridge.py"),
                           ("patch_web.py", index)):
        run = subprocess.run([sys.executable, str(HERE / script), str(target)],
                             capture_output=True, text=True)
        if run.returncode != 0:
            raise SystemExit(f"{script} failed on the pinned upstream:\n{run.stdout}\n{run.stderr}")
    return index


class Handler(http.server.SimpleHTTPRequestHandler):
    """Static server for the page directory; the API is intercepted in the browser."""

    def log_message(self, *args):  # keep the test output readable
        pass


def serve(directory: pathlib.Path) -> tuple[socketserver.TCPServer, str]:
    handler = functools.partial(Handler, directory=str(directory))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/index.html"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--html", help="already-built web/index.html")
    ap.add_argument("--upstream", help="pinned upstream directory (bridge.py + web/index.html)")
    ap.add_argument("--keep", action="store_true", help="keep the temporary build directory")
    args = ap.parse_args()

    import tempfile

    from playwright.sync_api import sync_playwright

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="rules-browser-"))
    try:
        if args.html:
            html = pathlib.Path(args.html).resolve()
        elif args.upstream:
            html = build_page(pathlib.Path(args.upstream).resolve(), tmp / "build")
        else:
            raise SystemExit("pass --html <built index.html> or --upstream <pinned dir>")

        httpd, url = serve(html.parent)
        result = Result()
        errors: list[str] = []
        posted: list[dict] = []
        state = {"config": json.loads(json.dumps(CONFIG))}

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                page = browser.new_page()
                page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
                page.on("console", lambda msg: errors.append(f"console.error: {msg.text}")
                        if msg.type == "error" else None)
                page.add_init_script(
                    "window.__unhandled=[];"
                    "window.addEventListener('unhandledrejection',"
                    "e=>window.__unhandled.push(String(e.reason&&e.reason.stack||e.reason)));"
                )

                def api(route):
                    path = route.request.url.split("://", 1)[-1]
                    path = "/" + path.split("/", 1)[1] if "/" in path else "/"
                    body = route.request.post_data
                    if path.startswith("/api/config"):
                        if route.request.method == "POST":
                            data = json.loads(body or "{}")
                            posted.append(data)
                            state["config"] = data.get("config", {})
                            route.fulfill(status=200, content_type="application/json",
                                          body=json.dumps({"ok": True}))
                        else:
                            route.fulfill(status=200, content_type="application/json",
                                          body=json.dumps(state["config"]))
                    elif path.startswith("/api/devices"):
                        route.fulfill(status=200, content_type="application/json",
                                      body=json.dumps({"ok": True, "count": len(DEVICES),
                                                       "devices": DEVICES}))
                    elif path.startswith("/api/aliases"):
                        route.fulfill(status=200, content_type="application/json",
                                      body=json.dumps({"aliases": {"light.a": "客厅主灯"}}))
                    else:
                        route.fulfill(status=200, content_type="application/json",
                                      body=json.dumps({"ok": True}))

                page.route("**/api/**", api)
                page.goto(url, wait_until="load")
                # the page opens on the dashboard; switch to 意图规则 like a user would
                page.click('[data-page="rules"]')
                page.wait_for_selector("#page-rules.active")
                # rows render collapsed (the grid is hidden), so wait for attachment
                page.wait_for_selector("#rule-list .rule-item", state="attached")

                rows = lambda: page.locator("#rule-list .rule-item")

                def expand(i):
                    """Click 编辑 only when the row is collapsed (toggleRule is a toggle)."""
                    row = rows().nth(i)
                    if "collapsed" in (row.get_attribute("class") or ""):
                        row.locator(".rule-expand").click()

                unhandled = lambda: page.evaluate("window.__unhandled")
                last_post = lambda: posted[-1] if posted else {}

                def clear_alert():
                    page.evaluate("() => document.querySelector('#rules-alert').classList.remove('show')")

                def click_save(locator):
                    """Click a save button and wait for the alert the save always shows."""
                    clear_alert()
                    locator.click()
                    page.wait_for_function(
                        "() => document.querySelector('#rules-alert').classList.contains('show')")

                # ---------- 1. render ----------
                result.eq("renders one row per rule", rows().count(), 3)
                titles = [rows().nth(i).locator(".rule-title").inner_text() for i in range(3)]
                result.eq("title falls back name -> pattern -> 新规则",
                          titles, ["客厅灯组", "关掉卧室灯", "睡觉模式"])
                summary0 = rows().nth(0).locator(".rule-summary").inner_text()
                result.check("multi-entity rule summary counts 2 devices",
                             "2 个设备" in summary0, summary0.replace("\n", " | "))
                result.check("rule without entity says 尚未选择设备",
                             "尚未选择设备" in rows().nth(2).locator(".rule-summary").inner_text())
                result.check("rows start collapsed",
                             "collapsed" in (rows().nth(0).get_attribute("class") or ""))

                # ---------- 2. 编辑 / 收起 ----------
                expand(0)
                result.check("编辑 expands the row",
                             "collapsed" not in (rows().nth(0).get_attribute("class") or ""))
                result.eq("expand button becomes 收起",
                          rows().nth(0).locator(".rule-expand").inner_text(), "收起")
                rows().nth(0).locator(".rule-expand").click()
                result.check("收起 collapses the row again",
                             "collapsed" in (rows().nth(0).get_attribute("class") or ""))
                result.eq("expand button back to 编辑",
                          rows().nth(0).locator(".rule-expand").inner_text(), "编辑")
                expand(0)

                # ---------- 3. device checkboxes / 全选 / 清空 ----------
                expand(1)
                picker1 = rows().nth(1).locator(".entity-picker")
                result.eq("single-entity rule pre-checks its device",
                          picker1.locator(".r-e:checked").count(), 1)
                picker1.locator(".r-e[value='light.c']").uncheck()
                posted.clear()
                click_save(rows().nth(1).locator(".rule-save"))
                rule1 = last_post().get("config", {}).get("intent_rules", [{}, {}])[1]
                result.eq("unchecking a device clears entity_id in the saved payload",
                          rule1.get("action", {}).get("entity_id"), "")
                picker1 = rows().nth(1).locator(".entity-picker")
                picker1.locator(".r-e[value='light.c']").check()

                expand(0)
                picker0 = rows().nth(0).locator(".entity-picker")
                result.eq("two entities pre-checked", picker0.locator(".r-e:checked").count(), 2)
                picker0.get_by_role("button", name="全选").click()
                result.eq("全选 checks every light device",
                          picker0.locator(".entity-count").inner_text(), "已选择 3 个设备")
                picker0.get_by_role("button", name="清空").click()
                result.eq("清空 unchecks everything",
                          picker0.locator(".entity-count").inner_text(), "已选择 0 个设备")
                # restore the two entities this rule started with
                picker0.locator(".r-e[value='light.a']").check()
                picker0.locator(".r-e[value='light.b']").check()
                result.eq("re-checking restores the two devices",
                          picker0.locator(".r-e:checked").count(), 2)

                # ---------- 4. entity search filter ----------
                picker0 = rows().nth(0).locator(".entity-picker")
                picker0.locator(".entity-search").fill("light.b")
                result.eq("entity search keeps only the match",
                          picker0.locator(".entity-option:visible").count(), 1)
                picker0.locator(".entity-search").fill("")
                result.eq("clearing entity search restores options",
                          picker0.locator(".entity-option:visible").count(), 3)

                # ---------- 5. rule search ----------
                page.fill("#rule-filter", "卧室")
                result.eq("rule search narrows to one row",
                          page.locator("#rule-list .rule-item:visible").count(), 1)
                page.fill("#rule-filter", "")
                result.eq("clearing rule search restores all rows",
                          page.locator("#rule-list .rule-item:visible").count(), 3)

                # ---------- 6. 新增 ----------
                page.get_by_role("button", name="+新增").click()
                result.eq("新增 appends a row", rows().count(), 4)
                result.check("new row starts expanded",
                             "collapsed" not in (rows().nth(3).get_attribute("class") or ""))
                result.eq("new row title is 新规则 #4",
                          rows().nth(3).locator(".rule-title").inner_text(), "新规则 #4")
                result.check("unsaved chip is visible on the new row",
                             rows().nth(3).locator(".rule-chip").is_visible())
                result.check("global 未保存 indicator is shown",
                             page.locator("#rules-dirty").is_visible())

                # ---------- 7. per-rule save keeps other drafts out ----------
                posted.clear()
                expand(0)
                rows().nth(0).locator(".r-p").fill("打开客厅的灯（改）")
                expand(1)
                rows().nth(1).locator(".r-p").fill("临时草稿-不该被单条保存提交")
                click_save(rows().nth(0).locator(".rule-save"))
                payload = last_post()
                saved = payload.get("config", {}).get("intent_rules", [])
                result.eq("single save posts the whole config once", len(posted), 1)
                result.eq("single save keeps three rules", len(saved), 3)
                result.eq("single save writes the edited rule",
                          saved[0]["pattern"], "打开客厅的灯（改）")
                result.eq("other rules' unsaved drafts are NOT submitted",
                          saved[1]["pattern"], "关掉卧室灯")
                result.check("single save keeps multi-entity action as an array",
                             isinstance(saved[0]["action"]["entity_id"], list),
                             repr(saved[0]["action"].get("entity_id")))
                result.check("single save keeps a single entity as a string",
                             isinstance(saved[1]["action"]["entity_id"], str),
                             repr(saved[1]["action"].get("entity_id")))
                result.eq("rule without entity keeps entity_id empty",
                          saved[2]["action"]["entity_id"], "")

                # ---------- 8. 保存全部 submits every draft ----------
                click_save(page.get_by_role("button", name="保存全部"))
                payload = last_post()
                all_rules = payload.get("config", {}).get("intent_rules", [])
                result.eq("save-all posts four rules", len(all_rules), 4)
                result.eq("save-all includes the other rule's draft",
                          all_rules[1]["pattern"], "临时草稿-不该被单条保存提交")
                result.check("save-all includes the new empty rule",
                             all_rules[3]["pattern"] == "" and all_rules[3].get("name") in (None, ""),
                             repr(all_rules[3]))
                page.wait_for_function(
                    "() => (document.querySelector('#rules-dirty').style.display || '') === 'none'")
                result.check("save-all clears the 未保存 indicator",
                             not page.locator("#rules-dirty").is_visible())

                # ---------- 9. delete ----------
                rows().nth(3).locator(".rule-del").click()
                result.eq("delete removes the row from the DOM", rows().count(), 3)
                click_save(page.get_by_role("button", name="保存全部"))
                after = last_post().get("config", {}).get("intent_rules", [])
                result.eq("saving after delete posts two... three rules", len(after), 3)
                result.check("deleted rule is gone from the payload",
                             "" not in [r.get("pattern") for r in after])
                rows().nth(0).locator(".rule-del").click()
                click_save(page.get_by_role("button", name="保存全部"))
                final_rules = last_post().get("config", {}).get("intent_rules", [])
                result.eq("deleting a persisted rule also persists", len(final_rules), 2)
                result.eq("the surviving rules are the remaining ones",
                          [r["pattern"] for r in final_rules],
                          ["临时草稿-不该被单条保存提交", "睡觉模式"])

                # ---------- 10. browser errors ----------
                pending = unhandled()
                result.check("no unhandled promise rejections", not pending, repr(pending))
                result.check("no pageerror / console.error", not errors, " | ".join(errors[:4]))
                browser.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    if result.failures:
        print(f"rules browser tests FAILED ({result.checks} checks, {len(result.failures)} failed)")
        for failure in result.failures:
            print(f"  - {failure}")
        return 1
    print(f"rules browser tests passed ({result.checks} checks, real Chromium)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
