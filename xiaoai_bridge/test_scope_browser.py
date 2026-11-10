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
    selection on screen;
  * 刷新音箱列表 calls the live Xiaomi endpoint instead of replaying the
    /api/status cache, including when the bridge never ran (empty cache), when
    Xiaomi answers with an empty list, and after switching accounts;
  * a 401/500 from that endpoint keeps both saved and unsaved picks, and a
    saved speaker that is no longer discovered keeps its slot and is saved back.
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
# What a different Xiaomi account returns after switching accounts.
ALT_DEVICES = [
    {"deviceID": "speaker-X", "name": "书房音箱", "hardware": "X08E"},
    {"deviceID": "speaker-Y", "name": "厨房音箱", "hardware": "X10A"},
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
        # The cached list /api/status reports (empty means "bridge never ran").
        "status_devices": json.loads(json.dumps(DEVICES)),
        # What the live Xiaomi refresh endpoint answers; overwritten per scenario.
        "speakers": {"status": 200,
                     "body": {"ok": True, "source": "xiaomi",
                              "devices": DEVICES,
                              "msg": "已从小米获取 3 台音箱"}},
        "speaker_calls": 0,
        "status_calls": 0,
        "expect_http_error": False,
    }
    default_speakers = json.loads(json.dumps(state["speakers"]))

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
            if path.startswith("/api/xiaomi/speakers"):
                state["speaker_calls"] += 1
                spec = state["speakers"]
                route.fulfill(status=spec["status"], content_type="application/json",
                              body=json.dumps(spec["body"]))
                return
            if path.startswith("/api/status"):
                state["status_calls"] += 1
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "running": False,
                                               "error": None,
                                               "devices": state["status_devices"]}))
                return
            if path.startswith("/api/devices"):
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "count": 0, "devices": []}))
                return
            if path.startswith("/api/version"):
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"version": "test"}))
                return
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True, "devices": DEVICES}))

        page.route("**/api/**", api)

    def scope_status(page):
        return page.eval_on_selector("#mi-scope-status", "el => el.textContent")

    def scope_notice(page):
        return page.eval_on_selector("#mi-scope-notice", "el => el.textContent")

    def checked_ids(page):
        return page.eval_on_selector_all(
            ".mi-scope-box:checked",
            "els => els.map(e => e.getAttribute('data-id'))")

    def refresh_count(page):
        return page.evaluate("() => globalThis.miScopeRefreshDone || 0")

    def wait_refresh(page, after=0):
        page.wait_for_function(
            "n => !globalThis.miScopeRefreshing"
            " && (globalThis.miScopeRefreshDone || 0) > n",
            arg=after, timeout=5000)

    def reload_page(page, mi, status_devices=None, speakers=None,
                    expect_http_error=False):
        """Re-open the panel with a fresh config / speaker backend state."""
        state["config"] = json.loads(json.dumps(BASE_CONFIG))
        state["config"]["xiaomi"] = json.loads(json.dumps(mi))
        state["revisions"] = {key: "fixture-" + key + "-0"
                              for key in ("xiaomi", "ha", "rules", "adv")}
        state["status_devices"] = json.loads(
            json.dumps(DEVICES if status_devices is None else status_devices))
        state["speakers"] = json.loads(
            json.dumps(default_speakers if speakers is None else speakers))
        state["speaker_calls"] = 0
        state["expect_http_error"] = expect_http_error
        page.reload()
        page.wait_for_function("typeof renderDeviceList === 'function'")
        page.click('.sidebar-item[data-page="xiaomi"]')
        page.wait_for_selector("#mi-scope-card", state="visible", timeout=5000)
        wait_refresh(page)

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
                if (state["expect_http_error"] and "Failed to load resource" in msg.text
                        and any(code in msg.text for code in ("401", "500", "502"))):
                    # The 401/500 refresh scenarios are deliberately provoked too.
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

            # Regression: the card used to stay hidden until a login response arrived,
            # so after a refresh there was no way back to the listening range.
            page.wait_for_selector("#mi-scope-card", state="visible", timeout=5000)
            wait_refresh(page)
            result.check("the scope card is visible without a fresh login",
                         page.is_visible("#mi-scope-card"))
            result.check("the card was filled from the live Xiaomi endpoint",
                         "已从小米获取" in scope_notice(page), scope_notice(page))

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

            # ══ refresh must ask Xiaomi, not replay the /api/status cache ═════
            # Each scenario reopens the panel, so the draft is rebuilt from the
            # saved config exactly like a browser reload.

            # 1) The bridge never ran: /api/status has no cached devices, but the
            #    backend still has the saved login and Xiaomi answers.
            reload_page(page, {"device_scope": "selected", "device_ids": ["speaker-A"]},
                        status_devices=[], speakers=default_speakers)
            result.check("empty cache: the live endpoint was called",
                         state["speaker_calls"] >= 1, str(state["speaker_calls"]))
            result.eq("empty cache: the saved speaker is checked",
                      checked_ids(page), ["speaker-A"])
            result.eq("empty cache: every live speaker is listed",
                      page.eval_on_selector_all(".mi-scope-box", "els => els.length"), 3)
            result.check("empty cache: nothing is marked missing",
                         "当前未发现" not in scope_status(page), scope_status(page))

            # 2) Xiaomi answers with an empty list: the saved slot must survive
            #    and a save must still carry it.
            reload_page(page, {"device_scope": "selected", "device_ids": ["speaker-A"]},
                        status_devices=[],
                        speakers={"status": 200,
                                  "body": {"ok": True, "source": "xiaomi",
                                           "devices": [],
                                           "msg": "小米账号下没有发现小爱音箱"}})
            result.eq("empty live list: the saved slot survives",
                      checked_ids(page), ["speaker-A"])
            result.check("empty live list: the slot is marked missing",
                         "当前未发现" in scope_status(page), scope_status(page))
            result.check("empty live list: the notice explains the empty answer",
                         "没有发现" in scope_notice(page), scope_notice(page))
            posted.clear()
            page.locator('button[onclick="saveXiaomi()"]').first.click()
            page.wait_for_timeout(300)
            saved_scope = (posted[0].get("config") or {}).get("xiaomi") if posted else {}
            result.eq("empty live list: saving still sends the saved id",
                      saved_scope.get("device_ids"), ["speaker-A"])

            # 3) 401 while refreshing: keep both the saved pick and the unsaved one.
            reload_page(page, {"device_scope": "selected", "device_ids": ["speaker-A"]})
            page.click("#mi-scope-mode-selected")
            page.locator(".mi-scope-box").nth(1).click()  # speaker-B, not saved yet
            result.eq("401 setup: two speakers are drafted",
                      len(checked_ids(page)), 2)
            state["speakers"] = {"status": 401,
                                 "body": {"ok": False, "code": "login_expired",
                                          "msg": "小米登录已失效，请重新登录"}}
            state["expect_http_error"] = True
            # The global fetch wrapper reloads on 401; this scenario is about the
            # endpoint handler, so opt out of that reload the way the wrapper does
            # after its first pass.
            page.evaluate("window.__bridgeAuthReloaded=true")
            before_refresh = refresh_count(page)
            page.click("#mi-scope-refresh")
            wait_refresh(page, before_refresh)
            result.eq("401: saved and unsaved picks both survive",
                      checked_ids(page), ["speaker-A", "speaker-B"])
            result.check("401: the failure is explained",
                         "登录" in scope_notice(page), scope_notice(page))
            result.check("401: the draft is still marked unsaved",
                         "未保存" in scope_status(page), scope_status(page))
            result.eq("401: the page was not reloaded", page.url, url)
            state["expect_http_error"] = False

            # 4) 500 while refreshing: the discovered list and the picks stay.
            reload_page(page, {"device_scope": "selected", "device_ids": ["speaker-A"]})
            page.click("#mi-scope-mode-selected")
            page.locator(".mi-scope-box").nth(2).click()  # speaker-C, not saved yet
            state["speakers"] = {"status": 500,
                                 "body": {"ok": False, "code": "xiaomi_unavailable",
                                          "msg": "向小米获取音箱列表失败"}}
            state["expect_http_error"] = True
            before_refresh = refresh_count(page)
            page.click("#mi-scope-refresh")
            wait_refresh(page, before_refresh)
            result.eq("500: the picks survive", checked_ids(page),
                      ["speaker-A", "speaker-C"])
            result.check("500: the notice says the old list was kept",
                         "已保留" in scope_notice(page), scope_notice(page))
            result.eq("500: the discovered list was not cleared",
                      page.eval_on_selector_all(".mi-scope-box", "els => els.length"), 3)
            state["expect_http_error"] = False

            # 5) A saved speaker is no longer in the account: keep its slot and
            #    keep saving it until the operator removes it.
            reload_page(page, {"device_scope": "selected",
                               "device_ids": ["speaker-A", "gone-1"]})
            result.eq("missing: three live speakers plus the saved slot",
                      page.eval_on_selector_all(".mi-scope-box", "els => els.length"), 4)
            result.check("missing: the gone speaker is marked on screen",
                         "当前未发现" in scope_status(page), scope_status(page))
            result.eq("missing: both saved ids are still checked",
                      checked_ids(page), ["speaker-A", "gone-1"])
            posted.clear()
            page.locator('button[onclick="saveXiaomi()"]').first.click()
            page.wait_for_timeout(300)
            saved_scope = (posted[0].get("config") or {}).get("xiaomi") if posted else {}
            result.eq("missing: saving keeps the unavailable speaker",
                      saved_scope.get("device_ids"), ["speaker-A", "gone-1"])

            # 6) Account switch: a new login renders the new account's speakers,
            #    then 刷新 must replace the list from Xiaomi instead of the stale
            #    /api/status cache, keeping the old saved pick as "not found".
            reload_page(page, {"device_scope": "selected", "device_ids": ["speaker-A"]})
            page.evaluate("devices => renderDeviceList(devices)", ALT_DEVICES)
            state["status_devices"] = json.loads(json.dumps(DEVICES))  # stale cache
            state["speakers"] = {"status": 200,
                                 "body": {"ok": True, "source": "xiaomi",
                                          "devices": ALT_DEVICES,
                                          "msg": "已从小米获取 2 台音箱"}}
            before = state["speaker_calls"]
            before_refresh = refresh_count(page)
            page.click("#mi-scope-refresh")
            wait_refresh(page, before_refresh)
            labels = " ".join(page.eval_on_selector_all(
                "#mi-scope-list label.rule-item", "els => els.map(e => e.textContent)"))
            result.check("switch: refreshing asked Xiaomi again",
                         state["speaker_calls"] > before)
            result.eq("switch: the new account's two speakers plus the old slot",
                      page.eval_on_selector_all(".mi-scope-box", "els => els.length"), 3)
            result.check("switch: the stale cached names are gone",
                         "客厅音箱" not in labels, labels)
            result.check("switch: the new account's names are shown",
                         "书房音箱" in labels and "厨房音箱" in labels, labels)
            result.eq("switch: only the old saved pick stays checked",
                      checked_ids(page), ["speaker-A"])
            result.check("switch: the old saved pick is marked missing",
                         "当前未发现" in scope_status(page), scope_status(page))

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
