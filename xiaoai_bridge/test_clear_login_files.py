#!/usr/bin/env python3
"""Regression: /api/xiaomi/clear-login must fail when a credential file survives.

`_delete_saved_login_files()` used to warn and continue, so a read-only or otherwise
undeletable auth.json/.mi.token could remain while the API reported success. The
helper now reports failures and `clear_xiaomi_login()` refuses to answer ok.
"""

from __future__ import annotations

import ast
import logging
import pathlib
import sys
import tempfile

FUNCTIONS = ("_delete_saved_login_files",)


def load(bridge_path: pathlib.Path, auth_path: pathlib.Path, token_path: pathlib.Path):
    source = bridge_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = [node for node in tree.body
              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS]
    missing = [name for name in FUNCTIONS if name not in {node.name for node in wanted}]
    assert not missing, f"bridge.py is missing {missing}"
    namespace = {
        "AUTH_PATH": auth_path,
        "TOKEN_PATH": token_path,
        "log": logging.getLogger("clear-login-test"),
        "json": __import__("json"),
        "asyncio": __import__("asyncio"),
    }
    module = ast.Module(body=wanted, type_ignores=[])
    exec(compile(module, str(bridge_path), "exec"), namespace)
    return namespace, source


def main() -> int:
    bridge = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "bridge.py")
    checks = 0
    failures = []

    def check(name, ok, detail=""):
        nonlocal checks
        checks += 1
        if not ok:
            failures.append(name)
            print(f"  FAIL {name} {detail}")

    with tempfile.TemporaryDirectory(prefix="clear-login-") as tmp:
        auth = pathlib.Path(tmp) / "auth.json"
        token = pathlib.Path(tmp) / ".mi.token"
        ns, source = load(bridge, auth, token)
        delete_files = ns["_delete_saved_login_files"]

        auth.write_text("{}", encoding="utf-8")
        token.write_text("{}", encoding="utf-8")
        removed, failed = delete_files()
        check("both credential files are removed", sorted(removed) == [".mi.token", "auth.json"], removed)
        check("removing existing files reports no failure", failed == [], failed)
        check("files are gone", not auth.exists() and not token.exists())

        # A directory in place of a file cannot be unlinked on any platform.
        token.mkdir()
        removed, failed = delete_files()
        check("an undeletable credential path is reported", bool(failed), failed)
        check("the surviving path is still there", token.exists())
        token.rmdir()

    check("clear_xiaomi_login refuses success while a file survives",
          "leftovers" in source and "未能删除全部登录凭据文件" in source)
    check("the API returns a revision so the page can refresh only that section",
          '"revision": _config_section_revision(load_config(), "xiaomi")' in source)

    print()
    if failures:
        print(f"clear-login file tests FAILED ({checks} checks, {len(failures)} failed)")
        return 1
    print(f"clear-login file tests passed ({checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
