# Changelog

## 1.0.10

### 安全
- 管理界面改为 HAOS Ingress 面板入口，沿用 Home Assistant 登录（仅管理员）；
  直连端口 47521 仍然可用，但**必须携带直连访问令牌**（`/data/config/.access_token`，
  也可在面板「高级」页点「显示直连令牌」查看；支持 `Authorization: Bearer` 调用）。
  日志只提示令牌文件位置，不打印令牌本身。
- 所有 `/api/*` 接口在服务端统一鉴权：未登录请求返回 401，前端按钮隐藏不再作为保护手段。
- `/api/config` 不再返回 `xiaomi.cookie_text`（内含 passToken / serviceToken），
  密码、HA Token、API Key 一并脱敏；掩码值再次保存时保留原值。
- 状态变更请求校验 `Origin`，会话 Cookie 使用 `HttpOnly` + `SameSite=Strict`，抑制跨站请求伪造。
- `uvicorn` 关闭代理头解析（`proxy_headers=False`），客户端无法用 `X-Forwarded-For`
  伪造 Ingress 信任的对端地址；非 ASCII 令牌按普通不匹配处理，不再返回 500。
- 关闭 FastAPI 的 `/docs`、`/redoc`、`/openapi.json`（不再对外暴露接口结构）。
- 扫码登录与轮询恢复 TLS 证书校验（移除 `ssl=False`）。
- 日志不再输出 `serviceToken` 片段。
- 不再把真实 token 文件路径交给 MiService：它会在登录失败时删除该文件。

### 登录稳定性
- Token 过期后的恢复路径统一走 `_relogin_and_refresh()`：用已保存的 passToken 换取新
  micoapi token，成功后同步 MiNAService 与轮询 Cookie；失败则指数退避（10s → 300s 上限），
  不再按轮询间隔高频重试，也不会覆盖或删除仍然有效的凭据。
- 仅扫码 / 仅 Cookie（没有账号密码）的场景可以正常启动并自动恢复。
- 保存凭据时只写非空字段并与磁盘已有值合并，避免一次不完整的刷新把 passToken 清空。
- 新增「清除登录凭据」按钮与 `POST /api/xiaomi/clear-login`：用户主动清除时同时删除
  `auth.json`、`.mi.token` 与配置中的 Cookie/密码，之后不会被旧页面保存逻辑自动恢复。

### 规则编辑
- 设备列表异步加载、切换页面不再重建规则列表，未保存的编辑不会被覆盖；
  新增「● 有未保存修改」提示。
- 保存失败不再重建列表、不再伪装成成功刷新，并显示具体失败原因。
- 规则收集限定在 `#rule-list` 内，修复访问设备/计划页面后保存规则报错的问题。
- 未选择实体时保存为空字符串（与上游一致），不再写入 `entity_id: []`。

### 构建与发布
- 新增 `requirements.lock.txt` 固定运行期依赖版本（上游 `requirements.txt` 只有 `>=`）。
- 构建期额外执行三套回归测试（登录/Token 模拟 + HTTP 鉴权 + Ingress 契约），任一失败即拒绝构建。
- CI 增加静态检查：全部文件编译、补丁禁止二次应用、禁止 `ssl=False` / `str(TOKEN_PATH)`。
- **CI 与正式发布拆分为两个工作流**：
  - `.github/workflows/ci.yml`：PR / push main / 手动触发，执行全部静态检查、宿主测试与 Docker
    构建加镜像内测试；**不登录 GHCR、无 `packages: write`、`push: false`**，并有守卫步骤在出现
    任何 registry 登录/推送时直接失败；
  - `.github/workflows/release.yml`：**仅手动触发**且**仅允许 `refs/heads/main`**，校验
    `inputs.version` 与 `config.yaml` 一致、拒绝覆盖已存在的 `<version>` 标签、发布前重跑全套测试，
    最后**只推送不可变的 `<version>` 标签**；`:latest` 不会被推送（`allow_latest=true` 直接失败）。
- 新增 `test_ingress_contract.py`：校验 `ingress_port` 与实际监听端口一致、前端在 Ingress 前缀下的
  相对路径解析、Supervisor 对端信任、直连令牌接口不向未授权请求泄露、旧 `/data` 可正常加载。
- 新增 `test_upgrade_data.py`：用 1.0.9 与 1.0.10 的真实补丁代码做「1.0.9 → 1.0.10 → 1.0.9」
  的 `/data` 往返，验证升级与回退的数据可操作性（附加组件安装器本身仍需真机验证）。
- 原有上游 Commit 固定机制（`UPSTREAM_REF`）与补丁架构（`patch_web.py` / `patch_auth.py`）保持不变。

## 1.0.9
- Show each intent rule as a compact summary of the phrase, action, and target devices.
- Add rule search and a visible match count; expand only the rule being edited.
- Preserve the multiple-entity selector and make new rules open for editing automatically.

## 1.0.8
- Allow one intent rule to target multiple Home Assistant entities of the same type.
- Limit rule collection to the rules editor, so visiting device or schedule pages does not break saving.
- Read the latest saved rules for each new voice command without restarting Xiaomi login.

## 1.0.7
- Start the bridge with a validated saved `micoapi` service token before attempting account login again.
- Validate device access before saving refreshed credentials, and prevent MiService from deleting the token file on failed login.
- Skip password login when only QR credentials are configured.
- Preserve fresh QR cookies when an older web page saves Xiaomi settings, and prefer the saved token when building polling cookies.
- Let QR-only users run the Xiaomi connection test; display failed QR login as a failure.

## 1.0.6
- Fix the Home Assistant connection test when the saved token is masked in the web UI.
- Reuse a saved token only for its original HA address; require a new token when changing the address.
- Prevent saving a changed HA address with the old masked token, and show the error in the web UI.
- Apply the same behavior to the HA services endpoint.

## 1.0.5
- Allow QR-only accounts to reconnect from saved `passToken` credentials without requiring a stored username and password.
- Reuse the saved QR identity when testing the Xiaomi connection after an app restart or update.

## 1.0.4
- Fix QR login reporting success while saving an empty Xiaomi `serviceToken`.
- Exchange QR `passToken` credentials for a fresh `micoapi` service token before saving.
- Refresh stale Cookie and cached passToken logins through the same validated flow.
- Stop exposing the Xiaomi user ID in QR success messages and logs.

## 1.0.3
- Convert the project into a proper Home Assistant App Store repository.
- Add `repository.yaml` and a dedicated `xiaoai_bridge/config.yaml`.
- Add HA entity drop-downs to intent rules.
- Filter entity choices by Home Assistant domain.
- Preserve an existing entity ID when it is temporarily unavailable.
- Pin the upstream XiaoAI HA Bridge revision for reproducible builds.
- Derive the GHCR tag from the app version automatically.
- Add build-time and post-build smoke checks before publishing the image.
