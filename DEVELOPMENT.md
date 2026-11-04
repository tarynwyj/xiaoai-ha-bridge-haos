# 开发与发布说明

这份文档面向**开发者与贡献者**：说明这个加载项是怎么构建的、补丁机制怎么工作、
测试怎么跑、CI 与发布是怎么分开的。普通用户请回到 [README.md](README.md) 和
[配置文档](xiaoai_bridge/DOCS.md)。

---

## 1. 项目结构

本仓库**不包含上游桥接器源码**。`xiaoai_bridge/` 是一个 Home Assistant OS 加载项定义
（`config.yaml`）+ 构建脚本（`Dockerfile`）+ 补丁脚本 + 测试，构建时才把上游代码拉下来打补丁。

```
xiaoai_bridge/
├── config.yaml            # HAOS 加载项元数据（version 是唯一版本源，也是镜像 tag）
├── Dockerfile             # 构建：拉固定 commit 的上游 → 打补丁 → 构建期跑测试
├── requirements.lock.txt  # 锁定运行期依赖（上游只有 >=，不可复现）
├── patch_auth.py          # 补丁：鉴权、登录/Token 恢复、轮询 Cookie、清理凭据
├── patch_web.py           # 补丁：Ingress 安全 fetch、规则编辑、清除凭据/显示令牌按钮
├── DOCS.md                # 用户文档（加载项界面里的「文档」页）
├── README.md              # 加载项商店里显示的简短说明
├── test_startup_patch.py  # 登录 / Token 刷新 / 凭据生命周期
├── test_api_auth.py       # HTTP 层鉴权与 CSRF
├── test_ingress_contract.py  # Ingress 契约（端口、相对路径、对端信任、令牌不泄露）
├── test_poll_auth.py      # 对话轮询的 Cookie 最小权限与重定向保护
├── test_sms_login.py      # 密码 + 短信验证 + STS 全链路（假 MiService/假 STS）
├── test_xiaomi_login.py   # 1.0.12：密码/验证码同会话登录 + 手动 Cookie 登录（假 aiohttp）
├── test_web_patch.py      # 规则页 JS 行为回归（Node + DOM stub）
├── test_upgrade_data.py   # 1.0.9 ⇄ 1.0.10 的 /data 升级与回退往返
├── test_tls_handshake.py  # 真实 TLS 握手：自签证书拒绝、可信证书通过（需 openssl）
├── test_schedule_automation.py  # 定时规则 → HA 自动化（HH:MM:SS + 1-7 整数星期）
├── test_clear_login_files.py    # 凭据文件删不掉时清除登录必须失败
├── test_rules_browser.py  # 规则页真实 Chromium 回归（Playwright + 本地 http.server）
└── test_workflow_policy.py   # 把「CI 不发布 / 发布仅手动」写成断言
```

构建输入固定为上游 commit `e4c82d4b4e872ff5d02d1baec089a6b4ad36734a`
（`Dockerfile` 与 `.github/workflows/*.yml` 里的 `UPSTREAM_REF`）。

## 2. 补丁机制

上游代码不可改，所有改动都通过字符串替换打进去：

```python
replace_exact(
    "人类可读的锚点说明",
    "上游代码里必须唯一出现一次的原文",
    "替换后的代码",
)
```

规则：

1. **必须恰好命中一次**，命中 0 次或多次都会让构建失败——这样上游一改代码就立刻暴露，
   而不是悄悄跳过补丁；
2. **禁止二次应用**：对已打补丁的文件再跑一次必须报错（CI 有专项检查）；
3. 补丁里的插入代码不要用 `"""`（外层是 `"""` 字符串，Python 3.11 会语法错），用 `'''`；
4. 新增补丁后，同步在对应测试里加断言；行为级测试优先于字符串比对。

`patch_auth.py` 目前 42 个锚点，`patch_web.py` 29 个。加补丁前先跑一遍
`python xiaoai_bridge/patch_auth.py <file>` 确认现有锚点仍然唯一。

## 3. 本地构建与测试

不需要 Docker 也能跑全部测试：把上游源码拉到临时目录、打补丁、再跑测试。

```sh
# 1) 取上游固定 commit 的两个文件
BASE=https://raw.githubusercontent.com/chenshuhe/xiaoai-ha-bridge/e4c82d4b4e872ff5d02d1baec089a6b4ad36734a
mkdir -p /tmp/pinned /tmp/app/web
curl -fsSL -o /tmp/pinned/bridge.py      "$BASE/bridge.py"
curl -fsSL -o /tmp/pinned/index.html     "$BASE/web/index.html"
cp /tmp/pinned/bridge.py  /tmp/app/bridge.py
cp /tmp/pinned/index.html /tmp/app/web/index.html

# 2) 打补丁
python xiaoai_bridge/patch_auth.py /tmp/app/bridge.py
python xiaoai_bridge/patch_web.py  /tmp/app/web/index.html
python -m py_compile /tmp/app/bridge.py

# 3) 跑测试（全部应当在没有网络、没有真实账号的情况下通过）
python xiaoai_bridge/test_workflow_policy.py
python xiaoai_bridge/test_startup_patch.py  /tmp/app/bridge.py
python xiaoai_bridge/test_poll_auth.py      /tmp/app/bridge.py
python xiaoai_bridge/test_sms_login.py      /tmp/app/bridge.py \
    --html /tmp/app/web/index.html
python xiaoai_bridge/test_api_auth.py       /tmp/app
python xiaoai_bridge/test_web_patch.py      --pristine /tmp/pinned/index.html
python xiaoai_bridge/test_web_patch.py      --html /tmp/app/web/index.html
python xiaoai_bridge/test_ingress_contract.py --bridge /tmp/app/bridge.py \
    --html /tmp/app/web/index.html --config xiaoai_bridge/config.yaml --repo-root .
python xiaoai_bridge/test_upgrade_data.py --pristine-bridge /tmp/pinned/bridge.py \
    --pristine-html /tmp/pinned/index.html --repo . --require-legacy
```

| 测试 | 覆盖内容 | 额外依赖 |
|---|---|---|
| `test_workflow_policy.py` | CI 不得发布、release 仅手动/main-only/无 `:latest` | PyYAML |
| `test_startup_patch.py` | 缓存 Token 启动、过期刷新、扫码登录、401 恢复与退避、凭据合并不清空 | 无 |
| `test_poll_auth.py` | 轮询携带 `userId`+`serviceToken`+目标 `deviceId`、不泄漏 `passToken`、重定向不跟随 | aiohttp |
| `test_sms_login.py` | 短信验证触发、验证链接归一化、恶意 STS 地址拒绝、STS 换 Token、凭据写入（`0600` 创建、部分失败回滚、旧文件读失败即中止）、失败不污染 `/data`、过期/重复/并发、前端按钮真实调用 `/verify` | aiohttp、Node（可缺）；`0600` 断言只在 Linux 生效 |
| `test_xiaomi_login.py` | 密码登录直接成功、触发验证码（不发短信）、60 秒冷却、同会话 8 步换 Token（断言所有 identity/verify 请求用同一个会话对象）、错误验证码计数与耗尽、TTL 过期、重新开始、unsupported 响应、`location` 白名单、设备探测通过后才写盘、手动 Cookie 登录（换取/直连/过期/缺字段/探测失败）、Cookie 与验证码状态互不影响、日志与响应不泄露密码/验证码/令牌、前端三个入口与按钮端点 | aiohttp、Node（可缺前端行为部分） |
| `test_api_auth.py` | 全部 `/api/*` 未授权 401、`/api/config` 脱敏、CSRF、Ingress 信任、清除凭据 | FastAPI |
| `test_web_patch.py` | 规则页未保存编辑保护、多实体、Ingress 相对路径、双击拒绝 | Node |
| `test_ingress_contract.py` | 端口契约、前端相对资源、对端信任、令牌不泄露、旧 `/data` 加载 | Node（可 `--no-node`） |
| `test_upgrade_data.py` | 用真实 1.0.9/1.0.10 补丁做 `/data` 往返与回退 | 需要 git 历史 |

镜像构建时会在镜像内再跑一遍 `test_startup_patch.py`、`test_api_auth.py`、
`test_ingress_contract.py --no-node`、`test_poll_auth.py`、`test_sms_login.py`、
`test_xiaomi_login.py`（镜像里没有 Node，前端行为部分自动跳过），任一失败即拒绝产出镜像。

## 4. CI 与发布

两个工作流，**故意分开**：

| 工作流 | 触发 | 做什么 | 会发布吗 |
|---|---|---|---|
| `.github/workflows/ci.yml` | `pull_request`（无路径过滤）、push 到 `main`（仅代码路径）、手动 | 编译 → workflow 策略自检 → 补丁重建与二次应用拒绝 → 8 套测试 → Docker 构建（`push: false`、`load: true`）→ 镜像内测试 → 镜像内 `index.html` 跑 Node 回归 → ingress 元数据校验 → 「CI 不可发布」结构守卫 | **不会** |
| `.github/workflows/release.yml` | 仅 `workflow_dispatch` | 强制 `refs/heads/main`、校验 `inputs.version == config.yaml`、拒绝覆盖已存在的 `<version>` 标签、重跑发布门禁后在 `linux/amd64` 构建、冒烟、**只推 `<version>`** | 只推不可变版本标签；`:latest` 永不推送 |

三条硬保证「PR 不会发布」：工作流里没有 registry 登录；权限只有 `contents: read`
（没有 `packages: write`）；末尾守卫读取 YAML 文本，一旦出现 registry 登录/推送/包写权限
就失败（`test_workflow_policy.py` 另外用解析后的结构再断言一次）。

### 发布流程

1. PR 合并到 `main`（合并本身不发布）；
2. 修改 `xiaoai_bridge/config.yaml` 的 `version`，合并后 HAOS 会提示有新版本；
3. **尽快**手动跑 **Release image**：Actions → Release image → Run workflow →
   branch `main`、`version` = `config.yaml` 里的版本 → 只推这个版本标签；
4. 若中途失败需要重推同一版本，显式勾 `overwrite_existing=true`。

> 注意顺序：`version` 一旦进 `main`，HAOS 就会向用户提示更新；此时镜像若还没发布，
> 用户点更新会失败（镜像不存在，加载项仍停在旧版本）。所以**先合并版本号，立刻发布**。

## 5. 版本号与镜像

- `xiaoai_bridge/config.yaml` 的 `version` 是唯一版本源：它同时决定加载项商店显示的版本、
  HAOS 拉取的镜像标签（`image:` + `:version`）和镜像里的 `io.hass.version` 标签；
- 发布前 `release.yml` 会断言 `inputs.version == config.yaml` 且 `image:` 与仓库路径一致；
- `requirements.lock.txt` 变更后要在 PR 里说明原因（上游依赖漂移会影响可复现性）。

## 6. 文档维护约定

| 文件 | 面向 | 写作要求 |
|---|---|---|
| `README.md` | 第一次接触项目的人 | 中文为主；只讲「能做什么 / 怎么装 / 怎么用」；技术细节链接到本文档 |
| `xiaoai_bridge/README.md` | 加载项商店里的用户 | 1 屏以内；打开方式、配置要点、常见操作 |
| `xiaoai_bridge/DOCS.md` | 使用中遇到问题的用户 | 完整配置教程、FAQ、升级与备份恢复 |
| `CHANGELOG.md` | 想知道更新了什么的人 | 按版本列「安全 / 稳定性 / 界面 / 构建与发布」 |
| `DEVELOPMENT.md` | 开发者与贡献者 | 本文件 |

改代码时如果改了用户可见行为（按钮、文案、默认值、权限模型），**同一个 PR 里**同步
`DOCS.md` 与 `CHANGELOG.md`；纯内部重构不必写。


## 7. 许可证维护

根目录 `LICENSE` 与 `THIRD_PARTY_NOTICES.md` 是授权及来源说明；
`xiaoai_bridge/` 下的同名文件是 Docker 构建副本，修改时须同步。
CI 和发布流程会逐字节比较副本，并检查构建镜像里的文件与根目录一致。

Dockerfile 将这两个文件放入 `/usr/share/licenses/xiaoai-ha-bridge-haos/`，
并将固定提交的上游 README 原样复制到
`/usr/share/licenses/xiaoai-ha-bridge/README.md`，保留其许可声明。
更新 `UPSTREAM_REF` 时须核对上游许可与版权信息，并同步更新第三方说明。
