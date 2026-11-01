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


POLL_COOKIE_KEYS = ("userId", "serviceToken")


def _poll_auth_cookies(device_id: str) -> dict:
    '''Authentication cookies attached to every conversation poll request.

    The shared CookieJar cannot be relied on here: aiohttp keeps the domain on the
    morsels, so injecting one SimpleCookie for several hosts stores everything
    under the first host and userprofile.mina.mi.com ends up with no credentials
    (that is the "connected but zero commands" bug). Attaching them per request
    also makes the selected speaker deviceId win over the stored one.

    Least privilege: only the two session cookies the conversation API actually
    needs are forwarded. passToken is a long lived login credential that belongs
    to the refresh path (account.xiaomi.com) and must never travel to the
    conversation host, even when the user pasted a full browser cookie.
    '''
    saved = _get_cookie(load_config()) or {}
    cookies = {key: saved[key] for key in POLL_COOKIE_KEYS if saved.get(key)}
    cookies["deviceId"] = device_id
    return cookies


def _apply_cookie_to_session(session, cfg: dict) -> bool:
    '''Push the token derived Cookie into the polling session CookieJar.

    Only the polling hosts receive these cookies, and only the session cookies
    (userId + serviceToken): account.xiaomi.com is the login/refresh domain and is
    filled by the login flow itself with the full cookie set, so passToken is
    never spread here.

    A plain dict is used per domain on purpose: reusing one SimpleCookie across
    domains stores every cookie under the first host (see _poll_auth_cookies).
    '''
    saved = _get_cookie(cfg) or {}
    cookies = {key: saved[key] for key in POLL_COOKIE_KEYS if saved.get(key)}
    if not cookies:
        return False
    try:
        import yarl
        for domain in ("mina.mi.com", "userprofile.mina.mi.com"):
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

# 注：短信验证端点在 1.0.11 被整体重写（见文件末尾 E 段）。新实现不再创建持有
# TOKEN_PATH 的 MiAccount，也不打印任何令牌，因此原来针对该端点的
# 「不得把 token 文件交给 MiService」与「不得打印 serviceToken」两条补丁随之移除；
# 同一约束仍由 Dockerfile / CI 的 grep 守卫保证。

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


# ════════════════ E: 短信验证登录闭环（1.0.11） ════════════════
replace_exact(
    'sms login: retry login instead of restarting the verification',
    r'''    global _pending_session
    if _pending_session and not _pending_session.closed:
        await _pending_session.close()

    try:
        from miservice.miaccount import get_random
''',
    r"""    global _pending_session
    # 短信验证期间再次点「测试连接」时，绝不能把正在等验证的会话关掉：
    # 那样会丢掉验证状态，让用户陷入「验证做完却一直登不上」的循环。
    if _pending_is_fresh():
        return {
            "ok": False,
            "need_verify": True,
            "verify_url": _pending_verify_url,
            "pending_seconds_left": max(0, SMS_PENDING_TTL - _pending_age_seconds()),
            "msg": "上一次短信验证还在进行中。\n①打开下方链接完成验证 ②回到本页把跳转地址粘贴到「短信验证」卡片并点「完成验证」",
        }
    await _close_pending_session()
    _clear_pending_login()

    try:
        from miservice.miaccount import get_random
""",
)

replace_exact(
    'sms login: remember when the verification was requested',
    r'''            _pending_session = s
            return {
                "ok": False,
                "msg": "密码验证通过，但需要手机验证码确认。\n①复制下方链接在浏览器中打开 ②完成短信验证 ③将跳转后页面的完整 Cookie 粘贴到下方输入框",
                "need_verify": True,
                "verify_url": verify_url,
            }
''',
    r"""            _pending_session = s
            _remember_pending_verification(verify_url)
            return {
                "ok": False,
                "msg": "密码验证通过，但需要手机验证码确认。\n①复制下方链接在浏览器中打开并完成验证 ②把跳转后的完整地址粘贴到「短信验证」卡片 ③点「完成验证」\n（也可以直接把完整浏览器 Cookie 粘贴到上方输入框后点「测试」）",
                "need_verify": True,
                "verify_url": verify_url,
                "pending_seconds_left": SMS_PENDING_TTL,
            }
""",
)

replace_exact(
    'sms login: sanitize the verification link',
    r'''            notif_url = resp.get('notificationUrl', '')
            if not notif_url:
                verify_url = "https://account.xiaomi.com"
            elif notif_url.startswith("https://") or notif_url.startswith("http://"):
                verify_url = notif_url
            elif notif_url.startswith("https//"):
                verify_url = notif_url.replace("https//", "https://", 1)
            elif notif_url.startswith("//"):
                verify_url = f"https:{notif_url}"
            elif notif_url.startswith("/"):
                verify_url = f"https://account.xiaomi.com{notif_url}"
            else:
                verify_url = f"https://account.xiaomi.com/{notif_url}"
''',
    r"""            verify_url = _normalize_verify_url(resp.get('notificationUrl', ''))
""",
)

replace_exact(
    'sms login: closed-loop verification endpoint',
    r'''@app.post("/api/test/xiaomi/verify")
async def xiaomi_verify(req: XiaomiVerifyRequest):
    """处理短信验证回调：支持 STS URL 或浏览器 Cookie"""
    global _pending_session
    try:
        from miservice import MiAccount, MiNAService
    except ImportError:
        return {"ok": False, "msg": "缺少依赖: pip install miservice"}

    if not _pending_login.get('username'):
        return {"ok": False, "msg": "请先点击「测试连接」触发验证流程"}

    username = _pending_login['username']
    password = _pending_login['password']
    pending = dict(_pending_login)

    if not req.cookie_text:
        return {"ok": False, "msg": "请粘贴短信验证完成后的跳转 URL"}

    # ── 方式A：短信验证回调 URL（自动获取 serviceToken）──
    if req.cookie_text.startswith("https://api2.mina.mi.com/sts"):
        log.info("检测到 STS 回调 URL，自动获取 serviceToken...")
        s = aiohttp.ClientSession()
        try:
            # 用 pending session 的 cookies 请求 STS 端点
            if _pending_session and not _pending_session.closed:
                import yarl
                for domain in ['account.xiaomi.com', '.mina.mi.com']:
                    pending = _pending_session.cookie_jar.filter_cookies(yarl.URL(f'https://{domain}/'))
                    s.cookie_jar.update_cookies(pending, yarl.URL(f'https://{domain}/'))
            async with s.get(req.cookie_text, timeout=aiohttp.ClientTimeout(total=15)):
                pass
            sts_cookies = {k: v.value for k, v in
                           s.cookie_jar.filter_cookies(yarl.URL('https://api2.mina.mi.com/')).items()}
            service_token = sts_cookies.get('serviceToken', '')
            if not service_token:
                raise Exception("未从 STS 响应中获取到 serviceToken，请重试")
            log.info("已获取 serviceToken: %s...", service_token[:30])
        except Exception as e:
            await s.close()
            return {"ok": False, "msg": f"STS 请求失败: {e}"}

        # 构造 token
        from miservice.miaccount import get_random
        account = MiAccount(s, username, password, str(TOKEN_PATH))
        account.token = {
            'deviceId': pending.get('deviceId', get_random(16).upper()),
            'userId': pending.get('userId', ''),
            'passToken': pending.get('passToken', ''),
            'micoapi': (pending.get('ssecurity', ''), service_token),
        }
        _save_auth_and_token(account)
        # 保存精简 cookie 到配置
        simple_cookie = f"userId={account.token['userId']}; passToken={account.token['passToken']}; serviceToken={service_token}; deviceId={account.token['deviceId']}"
        cfg = load_config()
        cfg.setdefault("xiaomi", {})["cookie_text"] = simple_cookie
        save_config(cfg)

        na = MiNAService(account)
        devices = await na.device_list()
        await s.close()
        _pending_login.clear()
        _pending_session = None
        if devices:
            return {"ok": True, "msg": f"短信验证完成！发现 {len(devices)} 台设备", "devices": devices}
        return {"ok": True, "msg": "短信验证完成，但未发现小爱音箱设备", "devices": []}

    # ── 方式B：浏览器 Cookie ──
    from http.cookies import SimpleCookie
    sc = SimpleCookie()
    sc.load(req.cookie_text)
    cookies = {k: m.value for k, m in sc.items()}

    if not cookies.get('userId'):
        return {"ok": False, "msg": "未识别有效内容。请确认粘贴的是短信验证后的跳转 URL（以 https://api2.mina.mi.com/sts 开头）或完整 Cookie"}
    if not cookies.get('passToken') or not cookies.get('serviceToken'):
        return {"ok": False, "msg": "Cookie 缺少 passToken 或 serviceToken，请复制完整 Cookie"}

    try:
        if _pending_session and not _pending_session.closed:
            await _pending_session.close()

        s = aiohttp.ClientSession()
        import yarl
        sc2 = SimpleCookie()
        for k, v in cookies.items():
            try: sc2[k] = v
            except Exception: pass
        for domain in ['account.xiaomi.com', '.mina.mi.com']:
            s.cookie_jar.update_cookies(sc2, yarl.URL(f'https://{domain}/'))

        from miservice.miaccount import get_random
        account = MiAccount(s, username, password, str(TOKEN_PATH))
        account.token = {
            'deviceId': cookies.get('deviceId', get_random(16).upper()),
            'userId': cookies['userId'],
            'passToken': cookies['passToken'],
            'micoapi': ('', cookies['serviceToken']),
        }
        na = MiNAService(account)
        devices = await na.device_list()
        _save_auth_and_token(account)
        cfg = load_config()
        cfg.setdefault("xiaomi", {})["cookie_text"] = req.cookie_text
        save_config(cfg)
        await s.close()
        _pending_login.clear()
        _pending_session = None
        if devices:
            return {"ok": True, "msg": f"登录成功，发现 {len(devices)} 台设备", "devices": devices}
        return {"ok": True, "msg": "登录成功，但未发现小爱音箱设备", "devices": []}
    except Exception as e:
        log.info(f"verify failed: {e}")
        return {"ok": False, "msg": f"验证失败: {e}"}''',
    r"""@app.post("/api/test/xiaomi/verify")
async def xiaomi_verify(req: XiaomiVerifyRequest):
    '''完成短信验证登录：粘贴回调地址，或直接粘贴完整浏览器 Cookie。'''
    try:
        from miservice import MiAccount, MiNAService  # noqa: F401  依赖检查
    except ImportError:
        return {"ok": False, "msg": "缺少依赖: pip install miservice"}

    raw = (req.cookie_text or "").strip()
    if not raw:
        return {
            "ok": False,
            "retry": True,
            "msg": "请粘贴短信验证完成后的跳转地址，或完整的浏览器 Cookie",
        }

    looks_like_url = raw.lower().startswith(("http://", "https://", "https//", "//"))
    if not looks_like_url:
        return await _finish_cookie_login(raw)

    lock = _sms_lock_obj()
    if lock.locked():
        return {"ok": False, "retry": True, "msg": "上一次验证仍在处理中，请稍候再试"}
    async with lock:
        return await _finish_sms_verification(raw)
""",
)

replace_exact(
    'sms login: pending state, strict URL checks and atomic persistence',
    r'''# ──────────────── HA 设备列表 ────────────────
''',
    r"""# ──────────────── 短信验证登录闭环（1.0.11） ────────────────
# 说明：本段代码由 patch_auth.py 插入 bridge.py，docstring 一律用三单引号，
# 以便它本身能被 patch_auth.py 的三双引号字面量包裹。

SMS_PENDING_TTL = 900          # 短信验证会话有效期（秒）
SMS_STS_HOST = "api2.mina.mi.com"
SMS_STS_PATH_PREFIX = "/sts"
SMS_VERIFY_URL_HOSTS = ("account.xiaomi.com", "api2.mina.mi.com")
SMS_SECRET_PATTERN = re.compile(r"[A-Za-z0-9_\-]{24,}")

_pending_created_at: float = 0.0
_pending_verify_url: str = ""
_sms_verify_lock: asyncio.Lock | None = None


class SmsVerifyError(Exception):
    '''Typed SMS verification failure: stable code for tests, safe message for the UI.'''

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _mask_secret_text(text: str) -> str:
    '''Drop token-like runs so a log line can never contain a credential.'''
    return SMS_SECRET_PATTERN.sub("***", str(text or ""))


def _redact_url(raw: str) -> str:
    '''Host and a short path prefix only: the query carries one-time signatures.'''
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(str(raw or "").strip())
    except Exception:
        return "<invalid-url>"
    path = parts.path or ""
    if len(path) > 12:
        path = path[:12] + "..."
    return f"{parts.scheme}://{parts.hostname or '?'}{path}"


def _normalize_verify_url(notif_url: str) -> str:
    '''Normalize the notificationUrl of the login response, or fall back safely.

    Xiaomi sometimes returns the path only ('/pass/...'), sometimes 'https//host'
    or '//host'. Only https URLs on a Xiaomi host are accepted: anything else
    falls back to the account page so a tampered response cannot send the user
    to an arbitrary site.
    '''
    from urllib.parse import urlsplit, urlunsplit
    fallback = "https://account.xiaomi.com"
    text = str(notif_url or "").strip()
    if not text:
        return fallback
    candidates = [text]
    lowered = text.lower()
    if lowered.startswith("https//"):
        candidates.insert(0, text.replace("https//", "https://", 1))
    elif text.startswith("//"):
        candidates.insert(0, "https:" + text)
    elif text.startswith("/"):
        candidates.insert(0, fallback + text)
    elif not lowered.startswith(("http://", "https://")):
        candidates.insert(0, fallback + "/" + text)
    for candidate in candidates:
        try:
            parts = urlsplit(candidate)
            port = parts.port
        except Exception:
            continue
        host = (parts.hostname or "").lower()
        if parts.scheme.lower() != "https" or parts.username or parts.password:
            continue
        if port not in (None, 443):
            continue
        allowed = host in SMS_VERIFY_URL_HOSTS \
            or host.endswith(".xiaomi.com") or host.endswith(".mi.com")
        if not allowed:
            continue
        return urlunsplit(("https", host, parts.path or "/", parts.query, ""))
    return fallback


def _pending_age_seconds() -> int:
    '''Age of the pending SMS verification, -1 when there is none.'''
    if not _pending_created_at:
        return -1
    return int(time.time() - _pending_created_at)


def _pending_is_fresh() -> bool:
    '''A SMS verification is still in flight and its session is usable.'''
    return bool(
        _pending_login.get("username")
        and _pending_session is not None
        and not _pending_session.closed
        and _pending_created_at > 0
        and (time.time() - _pending_created_at) < SMS_PENDING_TTL
    )


def _remember_pending_verification(verify_url: str) -> None:
    '''Mark the moment the SMS verification started (used for the TTL check).'''
    global _pending_created_at, _pending_verify_url
    _pending_created_at = time.time()
    _pending_verify_url = str(verify_url or "")


def _clear_pending_login() -> None:
    '''Drop the pending verification state; caller closes the session first.'''
    global _pending_session, _pending_created_at, _pending_verify_url
    _pending_login.clear()
    _pending_session = None
    _pending_created_at = 0.0
    _pending_verify_url = ""


async def _close_pending_session() -> None:
    '''Close (and forget) the session that held the SMS verification state.'''
    global _pending_session
    session = _pending_session
    _pending_session = None
    if session is not None and not session.closed:
        try:
            await session.close()
        except Exception as e:
            log.debug("关闭待验证会话失败: %s", e)


def _sms_lock_obj() -> "asyncio.Lock":
    '''Serialise SMS verification attempts so two requests cannot interleave.'''
    global _sms_verify_lock
    if _sms_verify_lock is None:
        _sms_verify_lock = asyncio.Lock()
    return _sms_verify_lock


def _validate_sts_url(raw: str) -> str:
    '''Strictly validate the pasted SMS verification callback URL.

    Only https://api2.mina.mi.com/sts... is accepted: no other host, no userinfo,
    no explicit port, no fragment. The query is kept because the exchange needs it,
    but it is never logged (see _redact_url).
    '''
    from urllib.parse import urlsplit, urlunsplit
    text = str(raw or "").strip()
    if not text:
        raise ValueError("请粘贴短信验证完成后的跳转地址")
    if any(ch in text for ch in " \t\r\n"):
        raise ValueError("地址中不能包含空格或换行，请复制完整的单行地址")
    parts = urlsplit(text)
    if parts.scheme.lower() != "https":
        raise ValueError("地址必须以 https:// 开头")
    if parts.username or parts.password:
        raise ValueError("地址不能包含用户名或密码信息")
    host = (parts.hostname or "").lower()
    if host != SMS_STS_HOST:
        raise ValueError("地址主机必须是 " + SMS_STS_HOST)
    try:
        port = parts.port
    except ValueError:
        raise ValueError("地址端口不合法")
    if port not in (None, 443):
        raise ValueError("地址只能使用标准 HTTPS 端口")
    path = parts.path or ""
    if not path.startswith(SMS_STS_PATH_PREFIX):
        raise ValueError("地址路径必须以 " + SMS_STS_PATH_PREFIX + " 开头")
    return urlunsplit(("https", host, path, parts.query, ""))


def _credentials_from_pending(pending: dict, service_token: str, ssecurity: str = "") -> dict:
    '''Build the credential dict from the pending login state, never from cookies.'''
    return {
        "userId": str(pending.get("userId") or "").strip(),
        "passToken": str(pending.get("passToken") or "").strip(),
        "deviceId": str(pending.get("deviceId") or "").strip(),
        "ssecurity": str(ssecurity or pending.get("ssecurity") or "").strip(),
        "serviceToken": str(service_token or "").strip(),
    }


def _missing_credentials(creds: dict) -> list:
    '''Keys that must be present before a credential set may be persisted.'''
    return [key for key in ("userId", "passToken", "serviceToken") if not creds.get(key)]


async def _retry_login_for_sms(username: str, password: str, session, pending: dict):
    '''Retry the password login on the pending session.

    Xiaomi remembers the completed verification for the account/session, so this is
    usually what turns a "securityStatus 16" login into a full token, and it never
    sends another SMS by itself. Returns a credential dict, or None when Xiaomi
    still asks for verification (or the retry failed for any reason).
    '''
    if not username or not password:
        return None
    from miservice import MiAccount
    from miservice.miaccount import get_random
    account = MiAccount(session, username, password, None)
    if not isinstance(getattr(account, "token", None), dict) or not account.token:
        account.token = {"deviceId": pending.get("deviceId") or get_random(16).upper()}
    try:
        resp = await account._serviceLogin("serviceLogin?sid=micoapi&_json=true")
        if resp.get("code") != 0:
            payload = {
                "_json": "true",
                "qs": resp["qs"],
                "sid": resp["sid"],
                "_sign": resp["_sign"],
                "callback": resp["callback"],
                "user": username,
                "hash": __import__("hashlib").md5(password.encode()).hexdigest().upper(),
            }
            resp = await account._serviceLogin("serviceLoginAuth2", payload)
        if resp.get("code") != 0:
            return None
        if resp.get("securityStatus", 0) & 16:
            return None
        if not resp.get("ssecurity") or not resp.get("location"):
            return None
        service_token = await account._securityTokenService(
            resp["location"], resp["nonce"], resp["ssecurity"]
        )
    except Exception as e:
        log.info("短信验证：重试登录未成功 (%s)", type(e).__name__)
        return None
    if not service_token:
        return None
    log.info("短信验证：重试登录已取得 micoapi serviceToken")
    return {
        "userId": str(resp.get("userId") or pending.get("userId") or ""),
        "passToken": str(resp.get("passToken") or pending.get("passToken") or ""),
        "deviceId": str(resp.get("deviceId") or pending.get("deviceId") or ""),
        "ssecurity": str(resp.get("ssecurity") or ""),
        "serviceToken": str(service_token),
    }


async def _exchange_sts_service_token(session, sts_url: str) -> str:
    '''Exchange the verification callback URL for a micoapi serviceToken.

    Runs on the *pending* login session because that is where the post-verification
    cookies live, and never follows a redirect: the URL carries one-time signatures
    and the cookies must not travel to another host.
    '''
    import yarl
    try:
        async with session.get(
            sts_url, timeout=aiohttp.ClientTimeout(total=15), allow_redirects=False
        ) as resp:
            if 300 <= resp.status < 400:
                log.warning("短信验证回调返回重定向 (%s)，已拒绝跟随", resp.status)
                raise SmsVerifyError(
                    "sts_redirect",
                    "短信验证地址返回了重定向，已停止跟随以避免凭据外泄。请重新点击「测试连接」再验证",
                )
            if resp.status >= 400:
                log.warning("短信验证回调返回 HTTP %s", resp.status)
                raise SmsVerifyError(
                    "sts_http",
                    "短信验证地址返回 HTTP " + str(resp.status) + "，链接可能已过期",
                )
    except SmsVerifyError:
        raise
    except Exception as e:
        log.warning("短信验证回调请求失败 (%s)", type(e).__name__)
        raise SmsVerifyError("sts_request", "访问短信验证地址失败（网络错误），请重试") from None
    jar = session.cookie_jar.filter_cookies(yarl.URL("https://" + SMS_STS_HOST + "/"))
    service_token = str(({k: m.value for k, m in jar.items()}).get("serviceToken") or "").strip()
    if not service_token:
        raise SmsVerifyError(
            "sts_no_token",
            "短信验证已完成，但回调没有返回 serviceToken。请重新点击「测试连接」再验证一次",
        )
    return service_token


async def _probe_devices_with_token(username: str, password: str, creds: dict) -> list:
    '''Validate a credential set by querying the speaker list. Never persists.'''
    from miservice import MiAccount, MiNAService
    from miservice.miaccount import get_random
    session = aiohttp.ClientSession()
    try:
        account = MiAccount(session, username, password, None)
        account.token = {
            "deviceId": creds.get("deviceId") or get_random(16).upper(),
            "userId": creds["userId"],
            "passToken": creds["passToken"],
            "micoapi": (creds.get("ssecurity") or "", creds["serviceToken"]),
        }
        devices = await MiNAService(account).device_list()
        return list(devices or [])
    except Exception as e:
        log.warning("短信验证凭据校验失败 (%s)", type(e).__name__)
        raise SmsVerifyError(
            "device_list", "登录凭据校验失败（无法获取小爱设备列表），请重试"
        ) from None
    finally:
        try:
            await session.close()
        except Exception:
            pass


def _persist_sms_credentials(creds: dict) -> None:
    '''Persist a fully validated credential set atomically (all or nothing).

    Both files are written to temporaries first and then swapped in, so a failure
    can never leave auth.json and .mi.token describing different logins.
    '''
    missing = _missing_credentials(creds)
    if missing:
        raise ValueError("凭据不完整: " + "、".join(missing))
    identity = {
        "passToken": creds["passToken"],
        "userId": creds["userId"],
        "deviceId": creds.get("deviceId") or "",
    }
    token_data = dict(identity)
    token_data["micoapi"] = [creds.get("ssecurity") or "", creds["serviceToken"]]

    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_auth = AUTH_PATH.with_name(AUTH_PATH.name + ".tmp")
    tmp_token = TOKEN_PATH.with_name(TOKEN_PATH.name + ".tmp")
    try:
        tmp_auth.write_text(json.dumps(identity, ensure_ascii=False), encoding="utf-8")
        tmp_token.write_text(json.dumps(token_data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_auth, AUTH_PATH)
        os.replace(tmp_token, TOKEN_PATH)
        try:
            os.chmod(TOKEN_PATH, 0o600)
        except OSError:
            pass
    finally:
        for stale in (tmp_auth, tmp_token):
            try:
                if stale.exists():
                    stale.unlink()
            except OSError:
                pass

    # cookie_text 只是给 _get_cookie 的提示，最后写；失败不影响已经保存的凭据
    try:
        cfg = load_config()
        cfg.setdefault("xiaomi", {})["cookie_text"] = (
            "userId=" + creds["userId"]
            + "; passToken=" + creds["passToken"]
            + "; serviceToken=" + creds["serviceToken"]
            + "; deviceId=" + (creds.get("deviceId") or "")
        )
        save_config(cfg)
    except Exception as e:
        log.warning("保存 cookie_text 失败 (%s)", type(e).__name__)


async def _finish_sms_verification(raw_url: str) -> dict:
    '''Complete a SMS login from the pasted verification callback URL.'''
    log_pending = _pending_is_fresh()
    try:
        sts_url = _validate_sts_url(raw_url)
    except ValueError as e:
        return {"ok": False, "retry": True, "msg": "短信验证地址不合法：" + str(e)}

    if not log_pending:
        await _close_pending_session()
        _clear_pending_login()
        return {
            "ok": False,
            "retry": False,
            "msg": "短信验证会话已失效（有效期 " + str(SMS_PENDING_TTL // 60)
                   + " 分钟），请重新点击「测试连接」获取新的验证链接",
        }

    session = _pending_session
    pending = dict(_pending_login)
    username = str(pending.get("username") or "")
    password = str(pending.get("password") or "")
    log.info("短信验证：收到回调地址 %s", _redact_url(sts_url))

    # 1) 先重试登录：验证状态保存在会话/账号上，回调地址只是一次性凭证
    creds = await _retry_login_for_sms(username, password, session, pending)

    # 2) 仍要求验证时，才用回调地址在同一个会话里换 serviceToken
    if not creds:
        try:
            service_token = await _exchange_sts_service_token(session, sts_url)
        except SmsVerifyError as e:
            log.warning("短信验证失败 (code=%s)", e.code)
            return {"ok": False, "retry": e.code != "sts_redirect", "msg": e.message}
        creds = _credentials_from_pending(pending, service_token)

    missing = _missing_credentials(creds)
    if missing:
        log.warning("短信验证：凭据不完整，缺少 %s", _mask_secret_text(",".join(missing)))
        return {
            "ok": False,
            "retry": True,
            "msg": "短信验证已完成，但缺少 " + "、".join(missing)
                   + "，请重新点击「测试连接」再验证一次",
        }

    # 3) 先验证可用性，通过后才落盘
    try:
        devices = await _probe_devices_with_token(username, password, creds)
    except SmsVerifyError as e:
        log.warning("短信验证：设备查询失败 (%s)", e.code)
        return {"ok": False, "retry": True, "msg": e.message}

    try:
        _persist_sms_credentials(creds)
    except Exception as e:
        log.warning("短信验证：保存凭据失败 (%s)", type(e).__name__)
        return {"ok": False, "retry": True, "msg": "短信验证成功，但保存凭据失败，请重试"}

    await _close_pending_session()
    _clear_pending_login()
    log.info("短信验证登录成功，发现 %d 台设备", len(devices))
    if devices:
        return {
            "ok": True,
            "msg": "短信验证完成，登录可用，发现 " + str(len(devices)) + " 台设备",
            "devices": devices,
            "verified": True,
        }
    return {
        "ok": True,
        "msg": "短信验证完成，登录可用，但未发现小爱音箱设备（可在设备列表页重试）",
        "devices": [],
        "verified": True,
    }


async def _finish_cookie_login(cookie_text: str) -> dict:
    '''Keep the original "paste a whole browser Cookie" login path working.'''
    from http.cookies import SimpleCookie
    sc = SimpleCookie()
    try:
        sc.load(cookie_text)
    except Exception:
        return {"ok": False, "retry": True, "msg": "Cookie 格式无法解析，请重新复制"}
    cookies = {k: m.value for k, m in sc.items()}
    if not cookies.get("userId"):
        return {
            "ok": False,
            "retry": True,
            "msg": "未识别有效内容。请粘贴短信验证后的跳转地址（以 https://api2.mina.mi.com/sts 开头）或完整 Cookie",
        }
    missing = [key for key in ("passToken", "serviceToken") if not cookies.get(key)]
    if missing:
        return {"ok": False, "retry": True, "msg": "Cookie 缺少 " + "、".join(missing) + "，请复制完整 Cookie"}

    mi_cfg = load_config().get("xiaomi", {})
    pending = dict(_pending_login or {})
    username = str(pending.get("username") or mi_cfg.get("username") or "")
    password = str(pending.get("password") or mi_cfg.get("password") or "")
    creds = {
        "userId": cookies["userId"],
        "passToken": cookies["passToken"],
        "deviceId": cookies.get("deviceId") or "",
        "ssecurity": cookies.get("ssecurity") or "",
        "serviceToken": cookies["serviceToken"],
    }
    try:
        devices = await _probe_devices_with_token(username, password, creds)
    except SmsVerifyError as e:
        log.warning("Cookie 登录校验失败 (%s)", e.code)
        return {"ok": False, "retry": True, "msg": e.message}

    try:
        _persist_sms_credentials(creds)
    except Exception as e:
        log.warning("Cookie 登录保存凭据失败 (%s)", type(e).__name__)
        return {"ok": False, "retry": True, "msg": "Cookie 登录成功，但保存凭据失败，请重试"}

    await _close_pending_session()
    _clear_pending_login()
    log.info("Cookie 登录成功，发现 %d 台设备", len(devices))
    if devices:
        return {
            "ok": True,
            "msg": "Cookie 登录成功，发现 " + str(len(devices)) + " 台设备",
            "devices": devices,
            "verified": True,
        }
    return {
        "ok": True,
        "msg": "Cookie 登录成功，但未发现小爱音箱设备（可在设备列表页重试）",
        "devices": [],
        "verified": True,
    }
# ──────────────── HA 设备列表 ────────────────
""",
)


path.write_text(source, encoding="utf-8")
print(f"Patched {path} with validated micoapi token exchange")
