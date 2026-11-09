"""Execute the complete built script as __main__, including FastAPI lifespan.

Only Uvicorn's blocking transport and external Xiaomi/HA calls are replaced.
Unlike AST-lifted unit tests, definitions after uvicorn.run are unavailable when
the hook runs, exactly as they are in the container's `python bridge.py` entry.
"""

import asyncio
import inspect
import json
import logging
import os
from pathlib import Path
import runpy
import sys
import tempfile
import time
from types import ModuleType
from unittest.mock import patch

import uvicorn
import yaml


def main():
    bridge = Path(sys.argv[1]).resolve()
    checks = []
    accounts = []

    def check(condition, message):
        if not condition:
            raise AssertionError(message)
        checks.append(message)

    class Account:
        def __init__(self, session, username, password, token_store=None):
            check(token_store is None, "token helper never gives MiService the credential file")
            check(username == password == "", "cached-token startup never supplies a password")
            self.session = session
            self.token = None
            accounts.append(self)

    class Service:
        def __init__(self, account):
            self.account = account

        async def device_list(self):
            return [{"deviceID": "entry-speaker", "hardware": "L05C", "name": "entry test"}]

    def server_run(app, **kwargs):
        # Capture the executing script's namespace BEFORE uvicorn.run returns.
        # Returning and inspecting runpy's result would mask this regression.
        ns = inspect.currentframe().f_back.f_globals
        check(ns["__name__"] == "__main__", "the complete script runs as the container entrypoint")
        for name in ("_build_token_account", "_miservice_auth_failure", "_schedule_once_state",
                     "_sync_schedule_clock", "_schedule_targets", "_create_ha_automation",
                     "toggle_automation", "ha_services"):
            check(callable(ns.get(name)), name + " exists before the server starts")
        check("_schedule_targets" in ns["_create_ha_automation"].__code__.co_names,
              "the verified schedule implementation overrides the upstream function before startup")
        for method, path in (("POST", "/api/automations/{automation_id}/toggle"),
                             ("POST", "/api/automations/cleanup"),
                             ("GET", "/api/ha/services")):
            check(any(getattr(route, "path", None) == path and method in getattr(route, "methods", ())
                      for route in app.routes), method + " " + path + " is registered before startup")
        check(kwargs.get("proxy_headers") is False, "entrypoint retains real-peer ingress checks")

        async def exercise_lifespan():
            polled = asyncio.Event()

            async def ha_connection(_self, _session):
                return True

            async def poll(_session, device_id, _hardware, _timestamp):
                check(device_id == "entry-speaker", "the complete bridge loop reaches the selected speaker")
                polled.set()
                await asyncio.Future()

            ns["HAClient"].test_connection = ha_connection
            ns["_get_latest_ask_from_xiaoai"] = poll
            async with app.router.lifespan_context(app):
                try:
                    await asyncio.wait_for(polled.wait(), timeout=3)
                    check(bool(accounts), "startup builds a token account through the real helper")
                    check(ns["bridge_state"]["connected"], "startup connects through the HA boundary")
                    check(ns["bridge_state"]["error"] is None, "startup has no NameError or other bridge error")
                    check(ns["bridge_state"]["running"], "startup remains running after entering polling")
                finally:
                    await ns["stop_bridge"]()
                    task = ns.get("_bridge_task")
                    if task:
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
            session = ns.get("_bridge_session")
            check(session is None or session.closed, "shutdown closes the HTTP session")

        asyncio.run(exercise_lifespan())

    previous_cwd = Path.cwd()
    old_handlers = list(logging.root.handlers)
    with tempfile.TemporaryDirectory(prefix="xiaoai-entrypoint-") as temporary:
        try:
            os.chdir(temporary)
            Path("config").mkdir()
            config = {"xiaomi": {"username": "", "password": "", "device_scope": "all"},
                      "homeassistant": {"url": "http://ha.invalid", "token": "entry-ha-token"},
                      "intent_rules": [], "poll_interval_seconds": 0.01}
            Path("config/config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
            identity = {"userId": "entry-user", "passToken": "entry-pass", "deviceId": "entry-device",
                        "ssecurity": "entry-security", "serviceToken": "entry-service"}
            Path("config/auth.json").write_text(json.dumps(identity), encoding="utf-8")
            token = {"userId": identity["userId"], "passToken": identity["passToken"],
                     "deviceId": identity["deviceId"], "issued_at": time.time(),
                     "micoapi": [identity["ssecurity"], identity["serviceToken"]]}
            Path("config/.mi.token").write_text(json.dumps(token), encoding="utf-8")
            fake_service = ModuleType("miservice")
            fake_service.MiAccount = Account
            fake_service.MiNAService = Service
            fake_account = ModuleType("miservice.miaccount")
            fake_account.get_random = lambda length: "E" * length
            with patch.object(uvicorn, "run", server_run), \
                 patch.dict(sys.modules, {"miservice": fake_service, "miservice.miaccount": fake_account}):
                runpy.run_path(str(bridge), run_name="__main__")
        finally:
            os.chdir(previous_cwd)
            # The full script creates a file handler even if basicConfig was
            # already configured. Close it before Windows removes the temp dir.
            for reference in list(logging._handlerList):
                handler = reference()
                if handler is not None and isinstance(handler, logging.FileHandler):
                    if str(getattr(handler, "baseFilename", "")).startswith(temporary):
                        logging.root.removeHandler(handler)
                        handler.close()
            for handler in list(logging.root.handlers):
                if handler not in old_handlers:
                    logging.root.removeHandler(handler)
                    handler.close()
    check(bool(checks), "entrypoint reached the server hook")
    print("complete entrypoint tests passed (%d checks)" % len(checks))


if __name__ == "__main__":
    main()
