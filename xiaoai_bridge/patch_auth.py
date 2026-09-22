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
        if account.token.get('passToken') and account.token.get('userId'):
            log.info("尝试 passToken 登录...")
            try:
                await _refresh_micoapi_token(
                    account,
                    account.token['userId'],
                    account.token['passToken'],
                    account.token.get('deviceId', get_random(16).upper()),
                )
                _save_auth_and_token(account)
                logged_in = True
            except Exception as e:
                log.warning("passToken 登录异常: %s，尝试其他方式", e)
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
                        account2 = MiAccount(_bridge_session, mi_cfg["username"], mi_cfg["password"], str(TOKEN_PATH))
                        await _refresh_micoapi_token(
                            account2,
                            cookies['userId'],
                            cookies['passToken'],
                            cookies.get('deviceId', get_random(16).upper()),
                        )
                        account = account2
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
                account = MiAccount(s, "", "", str(TOKEN_PATH))
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

path.write_text(source, encoding="utf-8")
print(f"Patched {path} with validated micoapi token exchange")
