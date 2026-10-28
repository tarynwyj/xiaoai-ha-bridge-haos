from pathlib import Path
import sys


path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/app/bridge.py")
source = path.read_text(encoding="utf-8")


def replace_exact(label: str, old: str, new: str) -> None:
    global source
    count = source.count(old)
    if count != 1:
        raise SystemExit(
            f"{label}: expected exactly one patch target, found {count}. "
            "The pinned upstream authentication flow may have changed."
        )
    source = source.replace(old, new, 1)


replace_exact(
    "restore complete cached micoapi token",
    """def _set_token(account, cfg: dict):
    \"\"\"从 auth.json 或 cookie 恢复 token（参考 xiaomusic set_token）\"\"\"
    mi_cfg = cfg.get("xiaomi", {})
    if AUTH_PATH.exists():
        try:
            with open(AUTH_PATH, encoding="utf-8") as f:
                user_data = json.load(f)
            account.token = {
                "passToken": user_data["passToken"],
                "userId": user_data["userId"],
                "deviceId": user_data.get("deviceId", ""),
            }
            log.info("已从 auth.json 恢复 token")
            return
        except Exception as e:
            log.warning("auth.json 读取失败: %s", e)

    cookie_text = mi_cfg.get("cookie_text", "")
    if cookie_text:
        from http.cookies import SimpleCookie
        sc = SimpleCookie()
        sc.load(cookie_text)
        cookies_dict = {k: m.value for k, m in sc.items()}
        account.token = {
            "passToken": cookies_dict.get("passToken", ""),
            "userId": cookies_dict.get("userId", ""),
            "deviceId": cookies_dict.get("deviceId", ""),
        }
""",
    """def _set_token(account, cfg: dict):
    \"\"\"Restore a complete micoapi token before falling back to account identity.\"\"\"
    for saved_path in (TOKEN_PATH, AUTH_PATH):
        if not saved_path.exists():
            continue
        try:
            data = json.loads(saved_path.read_text(encoding="utf-8"))
            if not data.get("userId") or not data.get("passToken"):
                continue
            restored = {
                "passToken": data["passToken"],
                "userId": str(data["userId"]),
                "deviceId": data.get("deviceId") or "",
            }
            micoapi = data.get("micoapi")
            if isinstance(micoapi, (list, tuple)) and len(micoapi) == 2 and micoapi[1]:
                restored["micoapi"] = tuple(micoapi)
            account.token = restored
            log.info("已从 %s 恢复小米登录信息", saved_path.name)
            return
        except Exception as e:
            log.warning("读取 %s 失败: %s", saved_path.name, e)

    cookie_text = (cfg.get("xiaomi", {}) or {}).get("cookie_text", "")
    if cookie_text:
        from http.cookies import SimpleCookie
        sc = SimpleCookie()
        sc.load(cookie_text)
        cookies = {key: morsel.value for key, morsel in sc.items()}
        if cookies.get("userId") and cookies.get("passToken"):
            account.token = {
                "passToken": cookies["passToken"],
                "userId": cookies["userId"],
                "deviceId": cookies.get("deviceId", ""),
            }
            if cookies.get("serviceToken"):
                account.token["micoapi"] = ("", cookies["serviceToken"])
""",
)

replace_exact(
    "prefer fresh token cookie over stale form data",
    """    if mi_cfg.get("cookie_text"):
        from http.cookies import SimpleCookie
        from aiohttp import CookieJar
        sc = SimpleCookie()
        sc.load(mi_cfg["cookie_text"])
        cookies_dict = {k: m.value for k, m in sc.items()}
        return cookies_dict

    if not TOKEN_PATH.exists():
        return None
""",
    """    if not TOKEN_PATH.exists():
        if mi_cfg.get("cookie_text"):
            from http.cookies import SimpleCookie
            sc = SimpleCookie()
            sc.load(mi_cfg["cookie_text"])
            return {k: m.value for k, m in sc.items()}
        return None
""",
)

replace_exact(
    "micoapi token helper",
    """async def bridge_loop():
""",
    """def _load_saved_xiaomi_identity() -> dict:
    \"\"\"Load only the account identity fields needed to refresh micoapi.\"\"\"
    for token_path in (TOKEN_PATH, AUTH_PATH):
        if not token_path.exists():
            continue
        try:
            data = json.loads(token_path.read_text(encoding="utf-8"))
            if data.get("userId") and data.get("passToken"):
                return {
                    "userId": str(data["userId"]),
                    "passToken": data["passToken"],
                    "deviceId": data.get("deviceId", ""),
                }
        except Exception as e:
            log.warning("读取已保存的小米登录信息失败: %s", e)

    cookie_text = (load_config().get("xiaomi", {}) or {}).get("cookie_text", "")
    if cookie_text:
        try:
            from http.cookies import SimpleCookie
            parsed = SimpleCookie()
            parsed.load(cookie_text)
            cookies = {key: morsel.value for key, morsel in parsed.items()}
            if cookies.get("userId") and cookies.get("passToken"):
                return {
                    "userId": cookies["userId"],
                    "passToken": cookies["passToken"],
                    "deviceId": cookies.get("deviceId", ""),
                }
        except Exception as e:
            log.warning("读取已保存的小米 Cookie 失败: %s", e)
    return {}


async def _refresh_micoapi_token(account, user_id: str, pass_token: str,
                                  device_id: str) -> str:
    \"\"\"Exchange an account passToken for a fresh micoapi serviceToken.\"\"\"
    if not user_id or not pass_token:
        raise ValueError("登录结果缺少 userId 或 passToken")

    account.token = {
        "deviceId": device_id,
        "userId": str(user_id),
        "passToken": pass_token,
    }
    resp = await account._serviceLogin("serviceLogin?sid=micoapi&_json=true")
    if resp.get("code") != 0:
        code = resp.get("code", "unknown")
        description = resp.get("description") or resp.get("desc") or "登录验证失败"
        raise RuntimeError(f"micoapi 登录失败 (code={code}): {description}")

    missing = [key for key in ("location", "nonce", "ssecurity") if not resp.get(key)]
    if missing:
        raise RuntimeError("micoapi 登录响应缺少字段: " + ", ".join(missing))

    service_token = await account._securityTokenService(
        resp["location"], resp["nonce"], resp["ssecurity"]
    )
    if not service_token:
        raise RuntimeError("小米 STS 未返回 micoapi serviceToken")

    account.token["userId"] = str(resp.get("userId", user_id))
    account.token["passToken"] = resp.get("passToken", pass_token)
    account.token["micoapi"] = (resp["ssecurity"], service_token)
    return service_token


def _token_snapshot(account) -> dict:
    '''Refresh must not destroy a working in-memory token when it fails.'''
    token = getattr(account, "token", None)
    if not isinstance(token, dict):
        return {}
    return {
        key: (list(value) if isinstance(value, tuple) else value)
        for key, value in token.items()
        if value
    }


def _restore_token(account, snapshot: dict) -> None:
    if snapshot:
        account.token = dict(snapshot)


def _poll_auth_cookies(device_id: str) -> dict:
    '''Authentication cookies attached to every conversation poll request.

    The shared CookieJar cannot be relied on here: aiohttp keeps the domain on the
    morsels, so injecting one SimpleCookie for several hosts stores everything
    under the first host and userprofile.mina.mi.com ends up with no credentials
    (that is the "connected but zero commands" bug). Attaching them per request
    also makes the selected speaker deviceId win over the stored one.
    '''
    cookies = dict(_get_cookie(load_config()) or {})
    cookies["deviceId"] = device_id
    return cookies


def _apply_cookie_to_session(session, cfg: dict) -> bool:
    '''Push the token derived Cookie into the polling session CookieJar.

    A plain dict is used per domain on purpose: reusing one SimpleCookie across
    domains stores every cookie under the first host (see _poll_auth_cookies).
    '''
    cookies = _get_cookie(cfg)
    if not cookies:
        return False
    try:
        import yarl
        for domain in ("account.xiaomi.com", "mina.mi.com", "userprofile.mina.mi.com"):
            session.cookie_jar.update_cookies(dict(cookies), yarl.URL(f"https://{domain}/"))
        return True
    except Exception as e:
        log.warning("写入轮询 Cookie 失败: %s", e)
        return False


async def _relogin_and_refresh(account, cfg: dict):
    '''Unified recovery after a 401: exchange the saved passToken for a new micoapi token.

    Works with QR / Cookie only installs (no username/password). Returns
    (na, devices) on success and None on failure; a failed refresh never writes
    to auth.json / .mi.token and leaves the previously working token in memory.
    '''
    from miservice import MiNAService
    from miservice.miaccount import get_random

    saved = _load_saved_xiaomi_identity()
    if not saved:
        log.warning("没有可用的扫码或 Cookie 凭据，无法自动刷新 Token")
        return None

    snapshot = _token_snapshot(account)
    try:
        await _refresh_micoapi_token(
            account,
            saved["userId"],
            saved["passToken"],
            saved.get("deviceId") or get_random(16).upper(),
        )
        na = MiNAService(account)
        devices = await na.device_list()
        if not devices:
            raise RuntimeError("设备列表为空")
    except Exception as e:
        log.warning("Token 刷新失败: %s", e)
        _restore_token(account, snapshot)
        return None

    _save_auth_and_token(account)
    return na, devices


async def bridge_loop():
""",
)

replace_exact(
    "allow QR-only bridge startup",
    """    if not mi_cfg.get("username") or not mi_cfg.get("password"):
        bridge_state["error"] = "小米账号未配置"
        bridge_state["running"] = False
        return
""",
    """    saved_identity = _load_saved_xiaomi_identity()
    if (not mi_cfg.get("username") or not mi_cfg.get("password")) and not saved_identity:
        bridge_state["error"] = "小米账号或扫码登录信息未配置"
        bridge_state["running"] = False
        return
""",
)

replace_exact(
    "use newly saved rules without restarting Xiaomi login",
    "                    action = parser.parse(query)\n",
    "                    action = IntentParser(load_config().get(\"intent_rules\", [])).parse(query)\n",
)

replace_exact(
    "allow saved QR identity in connection test",
    """    if not username or not password:
        return {"ok": False, "msg": "请先填写账号和密码"}

    global _pending_session
""",
    """    saved_identity = _load_saved_xiaomi_identity() if req.use_saved else {}
    if (not username or not password) and not saved_identity:
        return {"ok": False, "msg": "请先填写账号密码，或使用扫码登录"}

    global _pending_session
""",
)

replace_exact(
    "do not carry masked HA token to another address",
    """    if cfg.get("homeassistant", {}).get("token") == masked:
        cfg["homeassistant"]["token"] = existing.get("homeassistant", {}).get("token", "")
""",
    """    if cfg.get("homeassistant", {}).get("token") == masked:
        previous_ha = existing.get("homeassistant", {}) or {}
        old_url = (previous_ha.get("url") or "").strip().rstrip("/")
        new_url = (cfg["homeassistant"].get("url") or "").strip().rstrip("/")
        if new_url != old_url:
            return {"ok": False, "msg": "HA 地址已更改，请输入新 Token 再保存"}
        cfg["homeassistant"]["token"] = previous_ha.get("token", "")
""",
)

replace_exact(
    "preserve QR cookie after stale page save",
    """    if cfg.get("openai", {}).get("api_key") == masked:
        cfg["openai"]["api_key"] = existing.get("openai", {}).get("api_key", "")
    save_config(cfg)
""",
    """    if cfg.get("openai", {}).get("api_key") == masked:
        cfg["openai"]["api_key"] = existing.get("openai", {}).get("api_key", "")
    # Masked or empty means "keep what is stored". An explicit clear_login flag
    # (set only by the UI button / documented API call) is the sole way to wipe it.
    _merge_xiaomi_credentials(cfg, existing, masked)
    save_config(cfg)
""",
)

replace_exact(
    "keep MiService from deleting saved token on login failure",
    """        account = MiAccount(_bridge_session, mi_cfg["username"], mi_cfg["password"],
                           str(TOKEN_PATH) if TOKEN_PATH.parent.exists() else None)
""",
    """        # Persistence is handled by _save_auth_and_token after validation.
        # MiService deletes its token file when login fails, so never hand it TOKEN_PATH.
        account = MiAccount(_bridge_session, mi_cfg["username"], mi_cfg["password"], None)
""",
)

replace_exact(
    "test cached micoapi before re-login",
    """        na = None
        logged_in = False

        # 尝试1: 用 passToken 绕过密码步骤（xiaomusic 做法，不需要 SMS）
""",
    """        na = None
        devices = None
        logged_in = False

        # QR test already saved a micoapi serviceToken. Validate it before any
        # new account login, since a passToken can be rejected independently.
        if account.token.get('micoapi') and account.token['micoapi'][1]:
            try:
                na = MiNAService(account)
                devices = await na.device_list()
                logged_in = True
                log.info("已使用保存的 micoapi Token 登录")
            except Exception as e:
                log.warning("保存的 micoapi Token 不可用: %s", e)
                _set_token(account, cfg)
                na = None
                devices = None

        # 尝试1: 用 passToken 绕过密码步骤（xiaomusic 做法，不需要 SMS）
""",
)

replace_exact(
    "use saved QR identity in connection test",
    """        cookie_text = req.cookie_text or mi.get("cookie_text", "")
        if cookie_text and not cookie_text.startswith("https://"):
""",
    """        cookie_text = req.cookie_text or mi.get("cookie_text", "")
        if not cookie_text and saved_identity:
            cookie_text = (
                f"userId={saved_identity['userId']}; "
                f"passToken={saved_identity['passToken']}; "
                f"deviceId={saved_identity.get('deviceId', '')}"
            )
        if cookie_text and not cookie_text.startswith("https://"):
""",
)

replace_exact(
    "bridge passToken refresh",
    """        # 尝试1: 用 passToken 绕过密码步骤（xiaomusic 做法，不需要 SMS）
        if account.token.get('passToken'):
            log.info("尝试 passToken 登录...")
            try:
                ok = await account.login("micoapi")
                if ok:
                    logged_in = True
            except Exception as e:
                log.warning("passToken 登录异常: %s，尝试其他方式", e)
""",
    """        # 尝试1: 用 passToken 换取新的 micoapi serviceToken（不需要 SMS）
        if not logged_in and account.token.get('passToken') and account.token.get('userId'):
            log.info("尝试 passToken 登录...")
            try:
                await _refresh_micoapi_token(
                    account,
                    account.token['userId'],
                    account.token['passToken'],
                    account.token.get('deviceId', get_random(16).upper()),
                )
                na = MiNAService(account)
                devices = await na.device_list()
                _save_auth_and_token(account)
                logged_in = True
            except Exception as e:
                log.warning("passToken 登录异常: %s，尝试其他方式", e)
                _set_token(account, cfg)
                na = None
                devices = None
""",
)

replace_exact(
    "bridge cookie refresh",
    """                    if cookies.get('userId') and (cookies.get('passToken') or cookies.get('serviceToken')):
                        import yarl
                        sc2 = SimpleCookie()
                        for k, v in cookies.items():
                            try:
                                sc2[k] = v
                            except Exception:
                                pass
                        for domain in ['account.xiaomi.com', '.mina.mi.com']:
                            _bridge_session.cookie_jar.update_cookies(sc2, yarl.URL(f'https://{domain}/'))
                        account2 = MiAccount(_bridge_session, mi_cfg["username"], mi_cfg["password"], str(TOKEN_PATH))
                        account2.token = {
                            'deviceId': cookies.get('deviceId', get_random(16).upper()),
                            'userId': cookies['userId'],
                            'passToken': cookies.get('passToken', ''),
                        }
                        resp = await account2._serviceLogin('serviceLogin?sid=micoapi&_json=true')
                        if resp.get('code') != 0:
                            raise Exception(f"Cookie 验证失败: {resp.get('description','')}")
                        account2.token['userId'] = str(resp.get('userId', account2.token['userId']))
                        account2.token['micoapi'] = (
                            resp.get('psecurity', resp.get('ssecurity', '')),
                            cookies.get('serviceToken', ''),
                        )
                        account = account2
""",
    """                    if cookies.get('userId') and cookies.get('passToken'):
                        account2 = MiAccount(_bridge_session, mi_cfg["username"], mi_cfg["password"], None)
                        await _refresh_micoapi_token(
                            account2,
                            cookies['userId'],
                            cookies['passToken'],
                            cookies.get('deviceId', get_random(16).upper()),
                        )
                        candidate = MiNAService(account2)
                        candidate_devices = await candidate.device_list()
                        account = account2
                        devices = candidate_devices
""",
)

replace_exact(
    "avoid blank-password retry and validate password login",
    """        # 尝试3: 密码登录（会触发 SMS 验证）
        if not logged_in:
            log.info("尝试密码登录...")
            try:
                ok = await account.login("micoapi")
                if ok:
                    logged_in = True
            except Exception as e:
                log.warning("密码登录异常: %s", e)

        if not logged_in:
            bridge_state["error"] = ("登录失败，请打开配置页面 → 在「Cookie 登录」输入框中粘贴浏览器 Cookie → 点测试连接。"
                                     "Cookie 获取方法：浏览器打开 account.xiaomi.com → F12 → Application → Cookies → 复制全部")
""",
    """        # 尝试3: 只有提供真实账号密码时才尝试密码登录。
        if not logged_in and mi_cfg.get("username") and mi_cfg.get("password"):
            log.info("尝试密码登录...")
            try:
                password_account = MiAccount(
                    _bridge_session, mi_cfg["username"], mi_cfg["password"], None
                )
                password_account.token = {'deviceId': get_random(16).upper()}
                if await password_account.login("micoapi"):
                    candidate = MiNAService(password_account)
                    devices = await candidate.device_list()
                    account = password_account
                    na = candidate
                    logged_in = True
            except Exception as e:
                log.warning("密码登录异常: %s", e)

        if not logged_in:
            bridge_state["error"] = "小米登录失败。请在小米账号页重新扫码，然后再启动桥接器。"
""",
)

replace_exact(
    "reuse validated device list",
    """        # 获取设备列表
        try:
            devices = await na.device_list()
            if devices:
""",
    """        # 获取设备列表；登录验证时已经取得列表就不再重复请求。
        try:
            if devices is None:
                devices = await na.device_list()
            if devices:
""",
)

replace_exact(
    "test cookie refresh",
    """                    import yarl
                    sc2 = SimpleCookie()
                    for k, v in cookies.items():
                        try: sc2[k] = v
                        except Exception: pass
                    for domain in ['account.xiaomi.com', '.mina.mi.com']:
                        cs.cookie_jar.update_cookies(sc2, yarl.URL(f'https://{domain}/'))
                    acct = MiAccount(cs, username, password, str(TOKEN_PATH))
                    acct.token = {
                        'deviceId': cookies.get('deviceId', get_random(16).upper()),
                        'userId': cookies['userId'],
                        'passToken': cookies['passToken'],
                    }
                    resp = await acct._serviceLogin('serviceLogin?sid=micoapi&_json=true')
                    if resp.get('code') != 0:
                        raise Exception(f"Cookie 验证失败: {resp.get('description','')}")
                    acct.token['userId'] = str(resp.get('userId', acct.token['userId']))
                    acct.token['micoapi'] = (
                        resp.get('psecurity', resp.get('ssecurity', '')),
                        cookies.get('serviceToken', ''),
                    )
""",
    """                    acct = MiAccount(cs, username, password, str(TOKEN_PATH))
                    await _refresh_micoapi_token(
                        acct,
                        cookies['userId'],
                        cookies['passToken'],
                        cookies.get('deviceId', get_random(16).upper()),
                    )
""",
)

replace_exact(
    "QR micoapi exchange",
    """        # 检查是否扫码成功
        if lp_data.get("psecurity") and lp_data.get("userId"):
            # Step 3: 获取 serviceToken
            callback_url = lp_data.get("location", "")
            if callback_url:
                async with s.get(callback_url, headers=headers, ssl=False) as r:
                    pass
            sts_cookies = {k: v.value for k, v in
                           s.cookie_jar.filter_cookies(
                               __import__('yarl').URL('https://api2.mina.mi.com/')).items()}
            service_token = sts_cookies.get("serviceToken", "")
            if not service_token:
                service_token = lp_data.get("serviceToken", "")

            # 保存 auth 数据
            auth_data = {
                "passToken": lp_data["passToken"],
                "userId": str(lp_data["userId"]),
                "deviceId": _qr_state.get("device_id", ""),
                "ssecurity": lp_data.get("ssecurity", lp_data.get("psecurity", "")),
                "serviceToken": service_token,
            }
""",
    """        # 检查是否扫码成功
        if lp_data.get("psecurity") and lp_data.get("userId"):
            # Step 3: QR 登录取得的是 mijia 凭据；用 passToken 换取 micoapi token。
            try:
                from miservice import MiAccount
                # Never hand MiService a real token path: it deletes that file when a
                # login attempt fails. Persistence is done by _save_auth_and_token.
                account = MiAccount(s, "", "", None)
                service_token = await _refresh_micoapi_token(
                    account,
                    str(lp_data["userId"]),
                    lp_data.get("passToken", ""),
                    _qr_state.get("device_id", ""),
                )
            except Exception as e:
                _qr_state.clear()
                log.warning("QR 已确认，但 micoapi 换票失败: %s", e)
                await s.close()
                return {
                    "ok": False,
                    "done": True,
                    "msg": f"扫码已确认，但小爱服务登录失败: {e}",
                }

            # 只有取得非空 micoapi serviceToken 后才保存登录信息。
            auth_data = {
                "passToken": account.token["passToken"],
                "userId": account.token["userId"],
                "deviceId": account.token["deviceId"],
                "ssecurity": account.token["micoapi"][0],
                "serviceToken": service_token,
            }
""",
)

replace_exact(
    "QR success privacy",
    """            log.info("QR 扫码登录成功! userId=%s", auth_data["userId"])
            await s.close()
            return {"ok": True, "done": True, "msg": f"扫码登录成功! userId={auth_data['userId']}"}
""",
    """            log.info("QR 扫码登录成功，micoapi serviceToken 已保存")
            await s.close()
            return {"ok": True, "done": True, "msg": "扫码登录成功"}
""",
)

replace_exact(
    "resolve masked HA token for tests",
    """@app.post("/api/test/ha")
async def test_ha(req: TestHARequest):
    ha = HAClient(req.url, req.token)
""",
    """def _resolve_ha_credentials(req: TestHARequest) -> tuple[str, str]:
    \"\"\"Use the saved token only for the exact HA address it belongs to.\"\"\"
    url = req.url.strip().rstrip("/")
    if not url:
        raise ValueError("请先填写 Home Assistant 地址")

    token = req.token.strip()
    if token == "••••••••":
        saved = load_config().get("homeassistant", {}) or {}
        if url != (saved.get("url") or "").strip().rstrip("/"):
            raise ValueError("HA 地址已更改，请输入该地址的新 Token")
        token = saved.get("token") or ""
    if not token:
        raise ValueError("请先填写 Long-Lived Token")
    return url, token


@app.post("/api/test/ha")
async def test_ha(req: TestHARequest):
    try:
        url, token = _resolve_ha_credentials(req)
    except ValueError as e:
        return {"ok": False, "msg": str(e)}
    ha = HAClient(url, token)
""",
)

replace_exact(
    "resolve masked HA token for services",
    """@app.post("/api/ha/services")
async def get_ha_services(req: TestHARequest):
    ha = HAClient(req.url, req.token)
""",
    """@app.post("/api/ha/services")
async def get_ha_services(req: TestHARequest):
    try:
        url, token = _resolve_ha_credentials(req)
    except ValueError as e:
        return {"ok": False, "msg": str(e)}
    ha = HAClient(url, token)
""",
)

# ──────────────── 访问控制：鉴权基础（B/A） ────────────────
replace_exact(
    "auth imports",
    """import aiohttp
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
""",
    """import hashlib
import hmac
import ipaddress
import secrets
import aiohttp
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
""",
)

replace_exact(
    "disable public api docs",
    'app = FastAPI(title="小爱HA桥接器", lifespan=lifespan)',
    'app = FastAPI(title="小爱HA桥接器", lifespan=lifespan,\n'
    '              docs_url=None, redoc_url=None, openapi_url=None)',
)

replace_exact(
    "access control core",
    '''app.mount("/static", StaticFiles(directory=static_dir), name="static")
''',
    '''app.mount("/static", StaticFiles(directory=static_dir), name="static")

# ──────────────── 访问控制（HAOS Ingress + 直连令牌） ────────────────
# 本插件同时支持两种入口：
#   1) Home Assistant Ingress（推荐）：由 HA 完成用户认证后代理进来，
#      请求带 X-Ingress-Path 且来源必须是 Supervisor 地址；
#   2) 直连端口 47521：必须携带访问令牌（HttpOnly 会话 Cookie 或 Bearer）。
INGRESS_HEADER = "x-ingress-path"
SESSION_COOKIE = "xiaoai_bridge_session"
MASKED_VALUE = "••••••••"
AUTH_TOKEN_PATH = CONFIG_PATH.parent / ".access_token"
RELOGIN_BACKOFF_START = 10
RELOGIN_BACKOFF_MAX = 300
# Supervisor 在 hassio 网络中的固定地址 172.30.32.2（官方 Ingress 文档要求
# 只信任该地址）。直连端口可以伪造 X-Ingress-Path 头，因此必须校验传输层对端
# 地址，而不是只相信请求头；该网段内还有其它容器，不能放宽到整个 /23。
INGRESS_TRUSTED_PEERS = frozenset({"172.30.32.2"})
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _load_or_create_access_token() -> str:
    \'\'\'Load the direct-access token, creating a 256-bit one under /data on first run.\'\'\'
    try:
        if AUTH_TOKEN_PATH.exists():
            saved = AUTH_TOKEN_PATH.read_text(encoding="utf-8").strip()
            if saved:
                return saved
    except Exception as e:
        log.warning("读取访问令牌失败: %s", e)
    token = secrets.token_urlsafe(32)
    try:
        AUTH_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(str(AUTH_TOKEN_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            f.write(token + "\\n")
        log.info("已生成直连访问令牌并保存到 %s", AUTH_TOKEN_PATH)
    except Exception as e:
        log.warning("保存访问令牌失败: %s", e)
    return token


ACCESS_TOKEN = _load_or_create_access_token()
SESSION_VALUE = hmac.new(
    ACCESS_TOKEN.encode("utf-8"), b"xiaoai-bridge-session-v1", hashlib.sha256
).hexdigest()


def _peer_is_supervisor(host) -> bool:
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return str(addr) in INGRESS_TRUSTED_PEERS


def _request_is_ingress(request: Request) -> bool:
    \'\'\'True only for requests the Supervisor itself proxied into this add-on.\'\'\'
    if not request.headers.get(INGRESS_HEADER):
        return False
    peer = request.client.host if request.client else None
    return _peer_is_supervisor(peer)


def _constant_time_match(candidate) -> bool:
    \'\'\'Constant-time compare of a client supplied string with the access token.

    hmac.compare_digest rejects non-ASCII str, so a non-ASCII token must end up as
    a plain mismatch instead of an HTTP 500.
    \'\'\'
    if not isinstance(candidate, str) or not candidate:
        return False
    try:
        return hmac.compare_digest(candidate.encode("utf-8"), ACCESS_TOKEN.encode("utf-8"))
    except (UnicodeError, AttributeError, TypeError):
        return False


def _session_value_match(candidate) -> bool:
    if not isinstance(candidate, str) or not candidate:
        return False
    try:
        return hmac.compare_digest(candidate.encode("utf-8"), SESSION_VALUE.encode("utf-8"))
    except (UnicodeError, AttributeError, TypeError):
        return False


def _bearer_token_ok(request: Request) -> bool:
    header = (request.headers.get("authorization") or "").strip()
    candidates = []
    if header.lower().startswith("bearer "):
        candidates.append(header[7:].strip())
    direct = (request.headers.get("x-admin-token") or "").strip()
    if direct:
        candidates.append(direct)
    return any(_constant_time_match(candidate) for candidate in candidates)


def _session_cookie_ok(request: Request) -> bool:
    return _session_value_match((request.cookies.get(SESSION_COOKIE) or "").strip())


def _request_is_authenticated(request: Request) -> bool:
    if _request_is_ingress(request):
        return True
    return _session_cookie_ok(request) or _bearer_token_ok(request)


def _cross_site_request(request: Request) -> bool:
    \'\'\'Reject state-changing requests whose Origin does not match our Host.\'\'\'
    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        return False
    if origin == "null":
        return True
    host = (request.headers.get("host") or "").strip().lower()
    if not host:
        return True
    origin_host = origin.split("://", 1)[-1].split("/", 1)[0].lower()
    return origin_host != host


LOGIN_PAGE_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>XiaoAI HA Bridge · 登录</title>
<style>
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#0f1115;color:#e6e8ec;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{background:#171a21;border:1px solid #262b36;border-radius:14px;padding:28px 26px;max-width:420px;width:92%}
h1{font-size:18px;margin:0 0 10px}
p{font-size:13px;line-height:1.7;color:#a9b0bf;margin:0 0 12px}
code{background:#0f1115;padding:1px 5px;border-radius:4px;font-size:12px}
input{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:9px;border:1px solid #2b3240;
background:#0f1115;color:#e6e8ec;font-size:14px;margin:6px 0 12px}
button{width:100%;padding:10px 12px;border-radius:9px;border:0;background:#3b82f6;color:#fff;
font-size:14px;font-weight:600;cursor:pointer}
#err{color:#f87171;font-size:12px;min-height:16px;margin-top:10px}
.hint{font-size:12px;color:#7c8598}
</style></head>
<body><div class="card">
<h1>XiaoAI HA Bridge</h1>
<p>推荐从 <b>Home Assistant 侧边栏</b>打开本插件（Ingress 面板，无需令牌）。</p>
<p class="hint">直连访问需要令牌，文件位于容器内 <code>/data/config/.access_token</code>；
也可用 <code>ha addons log xiaoai_bridge</code> 查看启动日志。</p>
<input id="tk" type="password" autocomplete="current-password" placeholder="直连访问令牌">
<button id="go">登录</button>
<div id="err"></div>
</div>
<script>
(function(){
  var input=document.getElementById("tk"),err=document.getElementById("err");
  function login(){
    var token=(input.value||"").trim();
    if(!token){err.textContent="请输入访问令牌";return;}
    fetch("api/session",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({token:token})}).then(function(r){return r.json()}).then(function(d){
      if(d&&d.ok){location.replace("./");}else{err.textContent=(d&&d.msg)||"访问令牌不正确";}
    }).catch(function(e){err.textContent="登录失败: "+e.message;});
  }
  document.getElementById("go").addEventListener("click",login);
  input.addEventListener("keydown",function(e){if(e.key==="Enter")login();});
})();
</script>
</body></html>"""


@app.middleware("http")
async def _enforce_access_control(request: Request, call_next):
    path = request.url.path
    if path == "/api/session" and request.method.upper() == "POST":
        return await call_next(request)
    if _request_is_ingress(request):
        return await call_next(request)
    if not _request_is_authenticated(request):
        if request.headers.get(INGRESS_HEADER) and not _request_is_ingress(request):
            # Makes a mis-detected ingress deployment diagnosable instead of
            # silently showing the login page forever.
            log.warning(
                "收到 Ingress 请求但来源 %s 不是 Supervisor(172.30.32.2)，已拒绝；"
                "若侧边栏面板打不开，请用日志中的直连令牌访问 %d 端口",
                request.client.host if request.client else "unknown",
                WEB_PORT,
            )
        if path.startswith("/api/"):
            return JSONResponse(
                {
                    "ok": False,
                    "auth_required": True,
                    "msg": "未授权：请通过 Home Assistant Ingress 面板访问，或先登录直连令牌",
                },
                status_code=401,
            )
        if path in ("/", "/index.html"):
            return HTMLResponse(LOGIN_PAGE_HTML)
        return JSONResponse({"ok": False, "msg": "Not Found"}, status_code=404)
    if request.method.upper() in STATE_CHANGING_METHODS and _cross_site_request(request):
        log.warning("已拒绝跨站请求 %s %s (Origin=%s)", request.method, path,
                    request.headers.get("origin", ""))
        return JSONResponse({"ok": False, "msg": "跨站请求被拒绝"}, status_code=403)
    if (request.method.upper() in STATE_CHANGING_METHODS
            and _session_cookie_ok(request)
            and request.headers.get("x-requested-with") != "xiaoai-bridge"):
        # Belt and braces for the cookie mode: browsers can only set this header
        # same-origin (cross-origin would need a preflight we never answer).
        # Scripted API clients use Bearer tokens, so they are unaffected.
        return JSONResponse({"ok": False, "msg": "缺少 X-Requested-With 头，已拒绝"}, status_code=403)
    return await call_next(request)


class SessionRequest(BaseModel):
    token: str = ""


@app.post("/api/session")
async def create_session(req: SessionRequest):
    if not _constant_time_match(req.token.strip()):
        log.warning("直连登录失败：访问令牌不正确")
        return JSONResponse({"ok": False, "msg": "访问令牌不正确"}, status_code=401)
    response = JSONResponse({"ok": True})
    response.set_cookie(SESSION_COOKIE, SESSION_VALUE, httponly=True,
                        samesite="strict", max_age=7 * 24 * 3600, path="/")
    log.info("直连登录成功")
    return response


@app.post("/api/logout")
async def destroy_session():
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/auth/token")
async def reveal_direct_token(request: Request):
    \'\'\'Hand the direct-access token to an authenticated Home Assistant panel.

    Only reachable through HAOS Ingress (the Supervisor proxy), so the token can be
    recovered from the UI instead of being printed to the add-on log.
    \'\'\'
    if not _request_is_ingress(request):
        return JSONResponse(
            {"ok": False, "msg": "直连令牌只能在 Home Assistant 面板内查看"},
            status_code=403,
        )
    return {"ok": True, "token": ACCESS_TOKEN, "path": str(AUTH_TOKEN_PATH)}


def _delete_saved_login_files() -> list:
    removed = []
    for saved_path in (AUTH_PATH, TOKEN_PATH):
        try:
            if saved_path.exists():
                saved_path.unlink()
                removed.append(saved_path.name)
        except Exception as e:
            log.warning("删除 %s 失败: %s", saved_path.name, e)
    return removed


def _merge_xiaomi_credentials(cfg: dict, existing: dict, masked: str) -> None:
    \'\'\'Decide the stored Xiaomi login when the config page is saved.

    masked / empty  -> keep what is stored (a stale page must not wipe QR login)
    clear_login: true -> explicit user action, wipe saved credentials for real
    anything else  -> save the new value
    \'\'\'
    new_mi = cfg.get("xiaomi")
    if not isinstance(new_mi, dict):
        new_mi = {}
        cfg["xiaomi"] = new_mi
    old_mi = existing.get("xiaomi") or {}
    if new_mi.pop("clear_login", False):
        _delete_saved_login_files()
        new_mi["cookie_text"] = ""
        log.info("已按请求清除已保存的小米登录凭据")
        return
    if new_mi.get("password") in (masked, None):
        new_mi["password"] = old_mi.get("password", "")
    if new_mi.get("cookie_text") in (None, "", masked):
        new_mi["cookie_text"] = old_mi.get("cookie_text", "")
''',
)

# ──────────────── A: /api/config 不再回传敏感值 ────────────────
replace_exact(
    "mask xiaomi cookie in config response",
    """@app.get("/api/config")
async def get_config():
    cfg = load_config()
    # 脱敏
    if cfg.get("xiaomi", {}).get("password"):
        cfg["xiaomi"]["password"] = "••••••••"
    if cfg.get("homeassistant", {}).get("token"):
        cfg["homeassistant"]["token"] = "••••••••"
    if cfg.get("openai", {}).get("api_key"):
        cfg["openai"]["api_key"] = "••••••••"
    return cfg
""",
    """@app.get("/api/config")
async def get_config():
    cfg = load_config()
    # 脱敏：password、cookie_text（内含 passToken + serviceToken）、HA Token、API Key
    masked = MASKED_VALUE
    xiaomi = cfg.setdefault("xiaomi", {})
    if xiaomi.get("password"):
        xiaomi["password"] = masked
    if xiaomi.get("cookie_text"):
        xiaomi["cookie_text"] = masked
    for sensitive_key in ("passToken", "serviceToken", "ssecurity", "userId"):
        if xiaomi.get(sensitive_key):
            xiaomi[sensitive_key] = masked
    ha = cfg.setdefault("homeassistant", {})
    if ha.get("token"):
        ha["token"] = masked
    openai_cfg = cfg.setdefault("openai", {})
    if openai_cfg.get("api_key"):
        openai_cfg["api_key"] = masked
    return cfg
""",
)

# ──────────────── E: 用户主动清除已保存的登录凭据 ────────────────
replace_exact(
    "explicit credential clear endpoint",
    '''@app.get("/api/logs")
async def get_logs(n: int = 80):
    return log_records[-n:]
''',
    '''@app.get("/api/logs")
async def get_logs(n: int = 80):
    return log_records[-n:]


@app.post("/api/xiaomi/clear-login")
async def clear_xiaomi_login():
    \'\'\'User-initiated wipe of the saved Xiaomi login.

    Clears cookie_text / password in config.yaml and deletes auth.json and
    .mi.token. Because the files are gone, no later start-up can silently
    resurrect the credentials the user asked to remove.
    \'\'\'
    removed = _delete_saved_login_files()
    cfg = load_config()
    mi_cfg = cfg.setdefault("xiaomi", {})
    if mi_cfg.get("cookie_text"):
        mi_cfg["cookie_text"] = ""
        removed.append("cookie_text")
    if mi_cfg.get("password"):
        mi_cfg["password"] = ""
        removed.append("password")
    save_config(cfg)
    if bridge_state.get("running"):
        await stop_bridge()
    bridge_state["error"] = None
    log.info("已清除已保存的小米登录凭据: %s", ", ".join(removed) or "无")
    return {
        "ok": True,
        "removed": removed,
        "msg": "已清除已保存的小米登录凭据，请重新扫码登录",
    }
''',
)

# ──────────────── B: 恢复扫码登录流程的 TLS 证书校验 ────────────────
replace_exact(
    "qr service login tls",
    "async with s.get(url, headers=headers, ssl=False) as r:",
    "async with s.get(url, headers=headers) as r:",
)

replace_exact(
    "qr login url tls",
    "async with s.get(full_url, headers=headers, ssl=False) as r:",
    "async with s.get(full_url, headers=headers) as r:",
)

replace_exact(
    "qr long polling tls",
    "async with s.get(lp_url, headers=headers, ssl=False, timeout=timeout) as r:",
    "async with s.get(lp_url, headers=headers, timeout=timeout) as r:",
)

# ──────────────── D: Token 过期后的统一恢复路径 ────────────────
replace_exact(
    "backoff state before poll loop",
    '''        bridge_state["error"] = None

        while bridge_state["running"]:
''',
    '''        bridge_state["error"] = None
        relogin_backoff = RELOGIN_BACKOFF_START

        while bridge_state["running"]:
''',
)

replace_exact(
    "unified token refresh on expiry",
    """                    if need_relogin:
                        log.info("Token 已过期，尝试重新登录...")
                        try:
                            # 清除 session 的旧 cookie，避免小米返回精简响应导致 missing nonce
                            _bridge_session.cookie_jar.clear_domain("account.xiaomi.com")
                            _bridge_session.cookie_jar.clear_domain("mina.mi.com")
                            _set_token(account, cfg)
                            ok = await account.login("micoapi")
                            if ok:
                                _save_auth_and_token(account)
                                na = MiNAService(account)
                                # 刷新 cookie
                                cookies_dict = _get_cookie(cfg)
                                if cookies_dict:
                                    import yarl
                                    from http.cookies import SimpleCookie
                                    sc = SimpleCookie()
                                    for k, v in cookies_dict.items():
                                        try:
                                            sc[k] = v
                                        except Exception:
                                            pass
                                    for domain in ['account.xiaomi.com', '.mina.mi.com']:
                                        _bridge_session.cookie_jar.update_cookies(sc, yarl.URL(f'https://{domain}/'))
                                log.info("重新登录成功")
                            else:
                                log.warning("重新登录失败")
                        except Exception as e:
                            log.warning("重新登录异常: %s", e)
                        await asyncio.sleep(poll_interval)
                        continue
""",
    """                    if need_relogin:
                        log.info("Token 已过期，尝试自动刷新登录...")
                        # 清除 session 的旧 cookie，避免小米返回精简响应导致 missing nonce
                        _bridge_session.cookie_jar.clear_domain("account.xiaomi.com")
                        _bridge_session.cookie_jar.clear_domain("mina.mi.com")
                        recovered = await _relogin_and_refresh(account, cfg)
                        if recovered:
                            na, devices = recovered
                            # 刷新成功后同步 MiNAService 与轮询 Cookie
                            _apply_cookie_to_session(_bridge_session, cfg)
                            relogin_backoff = RELOGIN_BACKOFF_START
                            bridge_state["error"] = None
                            log.info("Token 刷新成功，已恢复轮询")
                            await asyncio.sleep(poll_interval)
                        else:
                            bridge_state["error"] = "小米登录已过期，正在自动重试（当前间隔 %ss）" % relogin_backoff
                            log.warning("Token 刷新失败，%ss 后重试；若持续失败请重新扫码登录",
                                        relogin_backoff)
                            await asyncio.sleep(relogin_backoff)
                            relogin_backoff = min(relogin_backoff * 2, RELOGIN_BACKOFF_MAX)
                        continue
""",
)

# ──────────────── D/E: 保存凭据时不覆盖有效值 ────────────────
replace_exact(
    "credential persistence must not blank out saved values",
    '''def _save_auth_and_token(account) -> None:
    """登录成功后保存 auth.json 和 .mi.token（参考 xiaomusic）"""
    if not hasattr(account, 'token') or not account.token:
        return
    try:
        AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        auth_data = {
            "passToken": account.token.get("passToken", ""),
            "userId": account.token.get("userId", ""),
            "deviceId": account.token.get("deviceId", ""),
        }
        with open(AUTH_PATH, "w", encoding="utf-8") as f:
            json.dump(auth_data, f, ensure_ascii=False)
        log.info("已保存 auth.json")
    except Exception as e:
        log.warning("保存 auth.json 失败: %s", e)
    try:
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            json.dump(account.token, f, ensure_ascii=False)
        log.info("已保存 .mi.token")
    except Exception as e:
        log.warning("保存 .mi.token 失败: %s", e)
''',
    '''def _read_saved_credential(saved_path) -> dict:
    try:
        if saved_path.exists():
            data = json.loads(saved_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        log.warning("读取 %s 失败: %s", saved_path.name, e)
    return {}


def _save_auth_and_token(account) -> None:
    """保存登录成功后得到的凭据，且永不把有效字段写成空值。

    只写回非空的身份字段；缺失字段回退到磁盘上已有的值，因此一次不完整的
    刷新不会销毁仍然可用的 passToken。
    """
    token = getattr(account, "token", None)
    if not isinstance(token, dict) or not token:
        return
    previous_token = _read_saved_credential(TOKEN_PATH)
    previous_auth = _read_saved_credential(AUTH_PATH)
    identity = {
        "passToken": token.get("passToken") or previous_token.get("passToken")
        or previous_auth.get("passToken") or "",
        "userId": str(token.get("userId") or previous_token.get("userId")
                      or previous_auth.get("userId") or ""),
        "deviceId": token.get("deviceId") or previous_token.get("deviceId")
        or previous_auth.get("deviceId") or "",
    }
    if not identity["userId"] or not identity["passToken"]:
        log.warning("跳过保存登录信息：凭据不完整，保留已有文件")
        return
    token_data = dict(identity)
    micoapi = token.get("micoapi") or previous_token.get("micoapi")
    if isinstance(micoapi, (list, tuple)) and len(micoapi) == 2 and micoapi[1]:
        token_data["micoapi"] = [micoapi[0], micoapi[1]]
    try:
        AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(AUTH_PATH, "w", encoding="utf-8") as f:
            json.dump(identity, f, ensure_ascii=False)
    except Exception as e:
        log.warning("保存 auth.json 失败: %s", e)
    try:
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            json.dump(token_data, f, ensure_ascii=False)
    except Exception as e:
        log.warning("保存 .mi.token 失败: %s", e)
''',
)

# ──────────────── D: 任何路径都不得让 MiService 接管并删除 .mi.token ────────────────
replace_exact(
    "cookie login must not hand MiService the token file",
    "acct = MiAccount(cs, username, password, str(TOKEN_PATH))",
    "acct = MiAccount(cs, username, password, None)",
)

replace_exact(
    "sms verify sts path must not hand MiService the token file",
    '''        from miservice.miaccount import get_random
        account = MiAccount(s, username, password, str(TOKEN_PATH))
        account.token = {
            'deviceId': pending.get('deviceId', get_random(16).upper()),
''',
    '''        from miservice.miaccount import get_random
        account = MiAccount(s, username, password, None)
        account.token = {
            'deviceId': pending.get('deviceId', get_random(16).upper()),
''',
)

replace_exact(
    "sms verify cookie path must not hand MiService the token file",
    """        from miservice.miaccount import get_random
        account = MiAccount(s, username, password, str(TOKEN_PATH))
        account.token = {
            'deviceId': cookies.get('deviceId', get_random(16).upper()),
""",
    """        from miservice.miaccount import get_random
        account = MiAccount(s, username, password, None)
        account.token = {
            'deviceId': cookies.get('deviceId', get_random(16).upper()),
""",
)

# ──────────────── D: 日志中不得出现 Token ────────────────
replace_exact(
    "never log the service token",
    '            log.info("已获取 serviceToken: %s...", service_token[:30])',
    '            log.info("已获取 micoapi serviceToken")',
)

# ──────────────── A: 启动时公布直连令牌（HA 附加组件日志界面可见，避免锁死） ────────────────
replace_exact(
    "advertise direct access token at startup",
    '''    log.info("Web 配置面板已启动 → http://0.0.0.0:%d", WEB_PORT)
''',
    '''    log.info("Web 配置面板已启动 → http://0.0.0.0:%d", WEB_PORT)
    log.info("推荐入口：Home Assistant 侧边栏 → XiaoAI HA Bridge（Ingress 面板，无需令牌）")
    log.info("直连 %d 端口需要访问令牌；令牌保存在 %s，可在面板内查看，"
             "删除该文件后重启会重新生成（日志中不打印令牌本身）",
             WEB_PORT, AUTH_TOKEN_PATH)
''',
)

# ──────────────── A: 不允许客户端用 X-Forwarded-For 改写对端地址 ────────────────
replace_exact(
    "never trust forwarded headers for the peer address",
    '    uvicorn.run(app, host="0.0.0.0", port=WEB_PORT, log_level="warning")',
    '    # proxy_headers=False: the Ingress trust check relies on the real TCP peer,\n'
    '    # so a client-supplied X-Forwarded-For must never be able to rewrite it.\n'
    '    uvicorn.run(app, host="0.0.0.0", port=WEB_PORT, log_level="warning",\n'
    '                proxy_headers=False)',
)

# ──────────────── D: 对话轮询必须显式携带认证 Cookie（PR #4） ────────────────
replace_exact(
    "poll requests carry the xiaomi auth cookies",
    '    cookies = {"deviceId": device_id}\n',
    """    cookies = _poll_auth_cookies(device_id)
""",
)

replace_exact(
    "poll must not follow redirects with auth cookies",
    "            r = await session.get(url, timeout=timeout, cookies=cookies)\n",
    """            # aiohttp re-sends an explicit cookies= dict on redirects, so a
            # redirect would hand userId/serviceToken to another host. Never
            # follow one from the polling endpoint.
            r = await session.get(url, timeout=timeout, cookies=cookies,
                                  allow_redirects=False)
            if 300 <= r.status < 400:
                log.warning("对话轮询被重定向 (%s → %s)，已停止跟随以保护认证 Cookie",
                            r.status, str(r.headers.get("Location", ""))[:120])
                return [], last_ts, False
""",
)

path.write_text(source, encoding="utf-8")
print(f"Patched {path} with validated micoapi token exchange")
