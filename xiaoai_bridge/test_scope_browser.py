#!/usr/bin/env python3
"""Real-browser regression for the 「监听范围」 card (Playwright + Chromium).

Runs against the built page (patch_web.py already applied) served by a local
static server; every /api/* call is answered by Playwright's route handler, so
no Xiaomi account or Home Assistant instance is involved.

    python xiaoai_bridge/test_scope_browser.py --html <built web/index.html>
    python xiaoai_bridge/test_scope_browser.py --upstream _local/02-upstream/pinned-e4c82d4

What it proves that the Node DOM stub cannot:
  * the card really renders in a browser, with the radios/checkbox states the
    user sees;
  * device names containing quotes/angle brackets are rendered as text and do
    not create elements;
  * clicking a checkbox once toggles once (no double toggle from label + input);
  * 保存 posts the scope in the request body, and a failed save keeps the
    selection on screen.
"""

from __future__ import annotations

import argparse
import http.server
import json
import pathlib
import socketserver
import subprocess
import sys
import tempfile
import threading

HERE = pathlib.Path(__file__).resolve().parent
try:  # the Windows console is GBK; never let a check line kill the run
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Three speakers: two share a name, one carries quotes and markup.
DEVICES = [
    {"deviceID": "speaker-A", "name": "客厅音箱", "hardware": "L05C"},
    {"deviceID": "speaker-B", "name": "客厅音箱", "hardware": "L06A"},
    {"deviceID": "speaker-C", "name": '卧室"<b>bold</b>', "hardware": "X10A"},
]

BASE_CONFIG = {
    "xiaomi": {"username": "u@example.com", "password": "••••••••"},
    "homeassistant": {"url": "", "token": ""},
    "intent_rules": [],
}


class Result:
    def __init__(self) -> None:
        self.checks = 0
        self.failures: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks += 1
        if not ok:
            self.failures.append(f"{name}{(' :: ' + detail) if detail else ''}")
            print(f"  FAIL {name}{(' :: ' + detail) if detail else ''}")
        else:
            print(f"  ok  {name}")
        return ok

    def eq(self, name: str, actual, expected) -> bool:
        return self.check(name, actual == expected, f"expected {expected!r}, got {actual!r}")


def build_page(upstream: pathlib.Path, workdir: pathlib.Path) -> pathlib.Path:
    workdir.mkdir(parents=True, exist_ok=True)
    html = workdir / "index.html"
    html.write_bytes((upstream / "web" / "index.html").read_bytes())
    subprocess.run([sys.executable, str(HERE / "patch_web.py"), str(html)],
                   check=True, capture_output=True)
    return html


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(directory: pathlib.Path):
    handler = lambda *a, **kw: Handler(*a, directory=str(directory), **kw)  # noqa: E731
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/index.html"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--html", help="already-built web/index.html")
    ap.add_argument("--upstream", help="pinned upstream directory")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="scope-browser-"))
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
    state = {
        "config": json.loads(json.dumps(BASE_CONFIG)),
        "revisions": {key: "fixture-" + key + "-0"
                      for key in ("xiaomi", "ha", "rules", "adv")},
        "fail_save": False,
        "counter": 0,
    }

    def open_page(page):
        def api(route):
            request = route.request
            path = request.url.split("://", 1)[-1]
            path = "/" + path.split("/", 1)[1] if "/" in path else "/"
            if path.startswith("/api/config"):
                if request.method == "POST":
                    data = json.loads(request.post_data or "{}")
                    posted.append(data)
                    if state["fail_save"]:
                        route.fulfill(status=500, content_type="application/json",
                                      body=json.dumps({"ok": False, "msg": "服务器炸了"}))
                        return
                    section = data.get("section")
                    if data.get("revision") != state["revisions"].get(section):
                        route.fulfill(status=409, content_type="application/json",
                                      body=json.dumps({"ok": False, "code": "config_conflict"}))
                        return
                    state["config"] = data.get("config", {})
                    state["counter"] += 1
                    state["revisions"][section] = "fixture-%s-%d" % (section, state["counter"])
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps({"ok": True,
                                                   "revision": state["revisions"][section]}))
                    return
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({**state["config"],
                                               "_section_revisions": state["revisions"]}))
                return
            if path.startswith("/api/devices"):
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "count": 0, "devices": []}))
                return
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True, "devices": DEVICES}))

        page.route("**/api/**", api)

    def scope_status(page):
        return page.eval_on_selector("#mi-scope-status", "el => el.textContent")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
            def capture_console(msg):
                if msg.type != "error":
                    return
                if state["fail_save"] and "500 (Internal Server Error)" in msg.text:
                    # The failure path is deliberately provoked below.
                    return
                errors.append(f"console: {msg.text}")

            page.on("console", capture_console)
            open_page(page)
            page.goto(url)
            page.wait_for_function("typeof renderDeviceList === 'function'")
            # The 小米账号 page is a tab; the scope card lives on it, so open it
            # the way a user would instead of poking at a hidden subtree.
            page.click('.sidebar-item[data-page="xiaomi"]')
            page.wait_for_selector("#page-xiaomi", state="visible")

            # ── default: everything, nothing pinned ──
            page.evaluate("devices => renderDeviceList(devices)", DEVICES)
            page.wait_for_selector("#mi-scope-card", state="visible")
            result.check("the scope card is visible",
                         page.is_visible("#mi-scope-card"))
            result.check("the all mode is the default",
                         page.is_checked("#mi-scope-mode-all"))
            result.eq("all discovered speakers are visibly checked in all mode",
                      page.eval_on_selector_all(".mi-scope-box", "els => els.filter(e => e.checked).length"), 3)
            result.eq("checkboxes are disabled in all mode",
                      page.eval_on_selector_all(".mi-scope-box", "els => els.filter(e => e.disabled).length"), 3)
            result.check("the status says everything is listened to",
                         "全部" in scope_status(page), scope_status(page))
            result.check("all mode explains how to enable individual selection",
                         "仅监听所选音箱" in scope_status(page), scope_status(page))
            result.eq("no device id was pinned into the form",
                      page.input_value("#mi-did"), "")

            # ── untrusted names render as text ──
            result.eq("a hostile device name creates no element",
                      page.eval_on_selector_all("#mi-scope-list b", "els => els.length"), 0)
            result.check("the hostile name is shown literally",
                         page.eval_on_selector_all(
                             ".mi-scope-box", "els => els.length") == 3)
            result.check("the raw name text survives",
                         page.eval_on_selector("#mi-scope-list", "el => el.textContent")
                         .count('卧室"<b>bold</b>') == 1,
                         page.eval_on_selector("#mi-scope-list", "el => el.textContent"))

            # ── selected mode: click once = toggle once ──
            page.click("#mi-scope-mode-selected")
            boxes = page.locator(".mi-scope-box")
            boxes.nth(0).click()
            result.eq("one click toggles exactly one box",
                      page.eval_on_selector_all(".mi-scope-box", "els => els.filter(e => e.checked).length"), 1)
            boxes.nth(2).click()
            result.eq("a second click adds the third speaker",
                      page.eval_on_selector_all(".mi-scope-box", "els => els.filter(e => e.checked).length"), 2)
            result.check("the status counts the selection",
                         "已选 2 台" in scope_status(page), scope_status(page))

            # ── re-render keeps the draft ──
            page.evaluate("devices => renderDeviceList(devices)", DEVICES)
            result.eq("re-rendering keeps the two picks",
                      page.eval_on_selector_all(".mi-scope-box", "els => els.filter(e => e.checked).length"), 2)

            # ── save posts the scope ──
            posted.clear()
            page.locator('button[onclick="saveXiaomi()"]').first.click()
            page.wait_for_timeout(400)
            result.eq("one save request was made", len(posted), 1)
            body = (posted[0].get("config") or {}).get("xiaomi") if posted else {}
            result.eq("the request carries the selected mode", body.get("device_scope"), "selected")
            result.eq("the request carries the two ids", body.get("device_ids"),
                      ["speaker-A", "speaker-C"])
            result.eq("the legacy single-device field is cleared", body.get("device_did"), "")
            result.check("the password field is still masked",
                         body.get("password") in ("••••••••", "******"), body.get("password"))

            # ── a failed save keeps the unsaved draft ──
            boxes.nth(1).click()  # add B: the draft now differs from disk
            result.eq("three boxes are picked before the failing save",
                      page.eval_on_selector_all(".mi-scope-box", "els => els.filter(e => e.checked).length"), 3)
            result.check("the page admits the change is unsaved",
                         "未保存" in scope_status(page), scope_status(page))
            state["fail_save"] = True
            posted.clear()
            page.locator('button[onclick="saveXiaomi()"]').first.click()
            page.wait_for_timeout(400)
            result.eq("the failing save was attempted", len(posted), 1)
            result.eq("the draft survived the failure",
                      page.eval_on_selector_all(".mi-scope-box", "els => els.filter(e => e.checked).length"), 3)
            result.check("the failure is still marked as unsaved",
                         "未保存" in scope_status(page), scope_status(page))
            result.check("the failure is reported inside the scope card",
                         "保存失败" in scope_status(page), scope_status(page))
            state["fail_save"] = False
            boxes.nth(1).click()  # drop B again for the zero-selection case

            # ── zero selection must not be savable ──
            boxes.nth(0).click()
            boxes.nth(2).click()
            posted.clear()
            page.locator('button[onclick="saveXiaomi()"]').first.click()
            page.wait_for_timeout(300)
            result.eq("an empty selection sends nothing", len(posted), 0)
            result.check("the reason is on screen",
                         "至少要选一台" in scope_status(page), scope_status(page))

            result.check("the page logged no error", not errors, "; ".join(errors[:3]))
            browser.close()
    finally:
        httpd.shutdown()

    print()
    if result.failures:
        print("scope browser tests FAILED (%d checks, %d failures)"
              % (result.checks, len(result.failures)))
        for item in result.failures:
            print("  - " + item)
        return 1
    print("scope browser tests passed (%d checks)" % result.checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
