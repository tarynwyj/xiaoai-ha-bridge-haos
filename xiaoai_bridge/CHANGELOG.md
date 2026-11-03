# Changelog

## 未发布（1.0.14：真机登录修复 + 定时任务表单）

### 登录（账号密码 + 验证码）
- 修复真机「验证码已通过，但恢复登录失败」：`_mi_service_login_verified` 复刻
  `miservice_fork==2.9.3` 时保留了 `else: cookies["passToken"] = ""`。aiohttp 合并显式
  `cookies=` 与会话 CookieJar 时同名键由显式值覆盖，这一行会把验证链刚写进 Jar 的
  `passToken` 清空，同会话 resume 于是变成匿名请求。现在 token 里没有 `passToken` 时不再
  发送该 Cookie（与 `miservice==3.0.1` 的 `_serviceLogin` 一致）。
- 新增 `_otp_session_identity()`：resume 前从会话 CookieJar 读回 `account.xiaomi.com` 上的
  `passToken` / `userId` 写进 token，不再依赖 aiohttp 的合并语义；日志只记录这两个凭据的
  "有/无"，从不记录值。
- resume 被拒时记录并展示小米返回的业务码与错误短语（日志
  `恢复登录被拒绝 (code=..., description=...)`，界面 `…（小米返回 code=…）`），下一轮真机
  可直接区分参数不合法、风控或需要再验证；日志与界面都不含凭据。

### 登录（手动 Cookie）
- 「验证 Cookie」改为**先换取、后兜底**：一律先用 `userId`/`passToken` 换 micoapi
  `serviceToken`，换取失败时才把粘贴内容里的 `serviceToken` 当兜底。浏览器
  `account.xiaomi.com` 的 `serviceToken` 属于网页登录（另一个 sid）且很快失效，之前
  「有就直接用」必然在设备列表校验处失败。
- 界面第 ③ 步与输入框占位符改为「只复制 userId 与 passToken，不要复制 serviceToken」，
  并说明原因；换取失败提示会建议去掉 `serviceToken` 后重试。
- 设备探测失败仍是显式错误且不写任何凭据文件（行为未变）。

### 定时任务（页面新建）
- 定时任务页新增「＋ 新建定时任务」表单：时间（24 小时制，`HH:MM`）、重复（每天 / 工作日 /
  周末 / 自定义星期）、`domain` / `service`、`entity_id`，点「创建」即可生成 HA 自动化。
  之前该页只读，只能靠语音「每天七点半打开热水器」创建。
- 新增 `POST /api/automations`：与语音路径**共用** `_create_ha_automation()`，因此 id 前缀
  （`xiaoai_bridge_`）、别名「小爱桥接-HH:MM 实体」、星期归一化（名称或 1–7 整数）与创建后的
  HA reload 完全一致。
- 输入校验前置：时间格式、`domain`/`service` 只接受 HA 英文名（如 `light` / `turn_on`），
  并核对 HA 自己的服务列表与实体列表；`entity_id` 必须形如 `light.xiaomi_xxx`，设备类服务没有
  实体会被拒绝，只有 `homeassistant` / `notify` / `persistent_notification` 允许留空且不再
  生成空 `target`；HA 未配置返回 400，创建失败返回 500。

### 审计修复（PR #10 review 反馈）
- **配置 ID 与实体 ID 分离**：HA 的自动化实体 `entity_id` 由别名生成（中文别名会变成
  `automation.xiao_ai_qiao_jie_...`），不能拿 `automation.xiaoai_bridge_` 前缀判断归属。
  列表改为读取实体 `attributes.id`（真正的配置 ID，也是删除接口要的 ID），返回新增的
  `config_id` 字段，页面删除按钮改用它；只显示本插件创建的自动化。
- **同一秒的两次创建不再合并**：`int(time.time())` 只有秒级精度，同一秒内建两条会互相覆盖；
  配置 ID 现带 `secrets.token_hex(4)` 随机后缀（已加并发创建回归证明不重复）。
- **星期严格校验**：表单里非法星期（如 `8`、`1,8`、拼错的英文名）返回 400，不再静默退化成
  「每天」；自定义重复留空由页面直接拦下。语音路径保持原有宽松规则。
- **服务/实体存在性校验**：创建前读取 HA 的 `/api/services` 与 `/api/states/<entity>`，服务名
  写错或实体不存在会 400 并给出具体原因；HA 读不到时跳过校验（创建本身会报连接错误）。
- **reload 失败不再谎报「创建失败」**：HA 已保存配置但 `automation/reload` 失败时返回
  `ok: true, reloaded: false` 并提示手动重载，避免用户重复提交；确实没保存才返回 500。
- **OTP CookieJar 按 resume URL 过滤**：Cookie 的 `Path` 也是作用域（`Path=/pass` 的
  `passToken` 在根路径过滤下不可见），恢复登录前改按
  `https://account.xiaomi.com/pass/serviceLogin?sid=micoapi&_json=true` 读取。
- **小米错误短语脱敏后入日志**：去掉控制字符、掩码 cookie 键值对与长随机串后再记录。
- **服务与目标域匹配校验**：`light.turn_on` 指到 `switch.x` 这类组合直接 400（`homeassistant.*`
  等跨域服务保留），避免「服务存在、实体存在、但两者不匹配」的自动化被创建。
- **创建结果三态化**：写入后区分 `created`（已保存、仅 reload 失败 → `reloaded:false`）、
  `missing`（确实没创建 → 500）、`unknown`（查询失败/超时 → **504 并提示先去 HA 核对**），
  不再把「无法确认」当作「肯定没创建」。
- **列表使用 `attributes.id` 的依据**：HA 的 `automation/entity.py` 把配置 ID 放在
  `capability_attributes()`（`{id: unique_id}`），它会出现在 `/api/states` 的 attributes 里；
  `config/automation.py` 的 `_write_value` 又固定写入 `{"id": config_key}`，`automation/util.py`
  再把它作为实体的 `unique_id`。回归里额外断言「没有 `attributes.id` 的状态被忽略」。
- **删除结果也区分 reload**：配置删除成功但 `automation/reload` 失败时返回
  `ok: true, reloaded: false` 并提示手动重载；删除本身就失败才 500；页面显示警告而不是「已删除」。
- **前端处理 504**：创建结果未知时页面显示「创建结果待确认，请先去 HA 核对，不要直接重复提交」，
  不再落进「✗ 创建失败」分支。
- **服务参数支持**：表单新增「服务参数（JSON，可留空）」；后端只接受扁平标量对象，并按 HA
  `/api/services` 的 `fields.required` 校验必填参数（如 `select.select_option` 的 `option`），
  缺参数直接 400 并在消息里点名，避免建出运行必然失败的自动化。
- **服务参数不能改写目标**：`entity_id` / `target` / `device_id` / `area_id` / `floor_id` /
  `label_id` 在服务参数里一律拒绝（目标只能来自独立实体输入框，也就是跨域校验用的那个值）；
  合并参数后还会再做一次不变量检查。
- **必填 `entity_id` 不再被无条件忽略**：只有独立实体字段确实有值时才认为满足
  `fields.required` 里的 `entity_id`；例如 `homeassistant.update_entity` 留空目标会 400。
- **删除请求本身结果未知**：删除请求失败时复用配置状态查询区分「配置已不存在（视为已删除）」、
  「配置仍在（500）」与「无法确认（504，提示先去 HA 核对）」，与创建路径同构。
- CI：Docker 镜像内回归清单补上 `test_schedule_automation.py`（此前只在主机阶段跑）。

### 测试
- `test_xiaomi_login.py`：把「没有 passToken 时发送空 passToken」的断言改为「不发送该
  Cookie」；verify 场景模拟验证链在会话里写入 `passToken`/`userId`，断言 resume 带上它们
  且日志不记录值；新增 resume 被拒场景（断言业务码与 description 进日志/界面、不写文件、
  清空 OTP 状态）；Cookie 场景改为「换取优先」，并新增「换取失败才兜底」与「兜底也失败
  不写盘」。
- `test_schedule_automation.py`：新增表单校验与 `POST /api/automations` 端点场景（合法请求生成
  与语音一致的 automation、时间/域/服务/实体/星期非法、HA 未配置、服务或实体不存在、reload
  失败、并发创建不重复均覆盖）；新增配置 ID/实体 ID 列表映射与「无实体不生成空 target」断言；
  `test_web_patch.py` 新增表单结构、`config_id` 删除与 reload 警告断言。
- `test_xiaomi_login.py`：新增真实 `aiohttp.CookieJar` 的 `Path=/pass` 作用域场景（断言按 resume
  URL 读取可见、按根路径不可见）与错误短语脱敏场景。

## 1.0.13

本版把 1.0.12 之后合入 `main` 的规则页修复一起发出去（NodeList 按钮失效、多设备选择状态丢失、
OTP 跳转链），并在其之上完成一轮安全与可靠性加固。

### 直连会话与管理 API 认证
- 直连登录改为**每次签发随机会话**：32 字节 URL-safe 随机值，服务端只保存 SHA-256 摘要，7 天过期、
  最多 128 个；「退出登录」会在服务端撤销该会话，旧 Cookie 即使被复制也不能再用。
- **加载项重启后直连浏览器需要重新登录**（会话只存在内存里）；Ingress 面板不受影响。
  注销只撤销浏览器会话，**不撤销脚本持有的 Bearer 令牌**，怀疑令牌泄露仍需轮换 `.access_token`。
- 文档补充直连端口是**明文 HTTP**：建议只在可信局域网使用、优先走 Ingress，不要暴露到公网。
- `/api/test/ha` 仍要求管理员权限，并限制协议与目标地址、不跟随重定向（HA 内网地址保持允许）。

### 配置写入（并发与一致性）
- **分区 CAS**：`GET /api/config` 返回 `xiaomi` / `ha` / `rules` / `adv` 四个分区的 revision，
  保存时需携带；分区已在其他窗口更新时返回 `409`，不覆盖现有数据。
- 旧客户端只提交部分键时不再删除未提交的字段；分区保存只合并本分区的输入。
- 小米凭据按分区合并：掩码值或空值保留磁盘原值，`clear_login` 只在 CAS 校验通过后才执行不可逆清理。

### 小米登录与凭据
- `auth.json` / `.mi.token` 统一用 `0600` 私有临时文件后原子替换，中途失败回滚到旧凭据。
- 启动时若两个凭据文件身份不一致（旧版本异常断电可造成），**拒绝混合登录并提示重新认证**。
- `_get_cookie` 只在 `cookie_text` 不完整时回退：要求含 `userId` + `serviceToken`，否则回退到有效的
  `.mi.token`；两者都没有则按认证失败处理。
- 保存的设备凭据必须含 `deviceId`，空值拒绝落盘。
- 清除登录：删除失败或文件仍残留时返回 `500` 并给出原因，不再谎报「已清除」；响应带小米分区 revision。
- 扫码登录与手动「测试连接」成功后刷新小米分区 revision，**紧跟其后的保存不会再 409**。

### 语音去重与延迟任务
- `last_timestamp.json` 改为临时文件 + `fsync` + 原子替换，损坏时**桥接器停止**而不是把旧语音重放；
  去重语义仍是「先记录时间戳、再执行动作」（最多一次）。
- 延迟任务登记在桥接器进程内，停止/重启时取消未执行任务并记录日志；**明确不跨重启**，
  需要断电后仍执行请用 HA 定时自动化。

### 定时任务（规则里的 `schedule`）
- 支持文档中的 `HH:MM:SS`（原来只接受 `HH:MM`），时间非法时记警告并拒绝，而不是静默失败。
- 星期支持名称、中文、逗号串以及文档里的 `[1..7]` 整数数组（1=周一）；原先对数组调用 `.lower()`
  会直接抛 `AttributeError`。

### 规则页
- **保存期间继续编辑不再丢**：单条保存只确认实际提交的快照，请求期间的新修改保留「未保存」chip 与
  全局标记；「保存全部」改为原地刷新，不再用陈旧快照重建整页，期间新增/编辑/删除的规则都保留。
- 「其他(JSON)」解析失败、或试图覆盖 `domain` / `service` / `entity_id` / `reply` 时**拒绝保存并提示**；
  合法 JSON（如 `{"delay_minutes":5}`）照常并入动作。
- 清除小米登录只刷新小米分区，不再 `loadConfig()` 覆盖高级设置/HA 等页面里未保存的输入。

### 构建与测试
- CI 的禁用模式检查改为在**重新构建后的 `bridge.py`** 上用显式 `if grep … then exit 1` 判断
  （原来的 `! grep` 在 `set -e` 下不会让步骤失败，检查形同虚设）。
- 新增真实 TLS 握手测试（自签证书拒绝、可信证书通过）、定时规则生成测试、清除凭据文件校验测试；
  Chromium 套件扩展到 108 项检查（含保存竞态、JSON 校验、清除登录范围）。
- `test_web_patch.py` 的 DOM 桩改为与浏览器一致的 NodeList 语义，避免再次掩盖浏览器专属缺陷。

### 其他
- 删除 `_merge_xiaomi_credentials()` 中不可达的 `clear_login` 分支（`post_config` 会在合并前处理该标志，
  并保证它不会被写进 `config.yaml`）。

## 1.0.12

### 登录（账号密码 + 验证码、手动 Cookie、界面重构）
- 新增「账号密码登录」：`POST /api/test/xiaomi/otp/start` 用填写的账号密码做**一次**密码登录
  （`serviceLogin?sid=micoapi` → `serviceLoginAuth2`，`qs/sid/_sign/callback/user/md5(password)`
  与 MiService 一致）。小米不要求二次验证时直接换 serviceToken → 校验设备列表 → 原子保存凭据，
  返回 `{ok:true, verified:true, devices:[...]}`。
- 需要二次验证时，按 MiService 2.4+/3.x 的 8 步协议在**同一个 aiohttp 会话**内完成：
  打开 `notificationUrl`（authStart）→ `/identity/list` 取 `flag`（4=手机 / 8=邮箱）→
  用户点「获取验证码」后 `verify<Method>?_flag=` → `sendPhoneTicket`（**只有用户点击才发短信**）→
  `verify<Method>?_dc=` 提交验证码 → 跟随响应里的 `location` → 同一会话内重跑 `serviceLogin` →
  `_securityTokenService`（带 clientSign）→ 设备列表校验 → 复用 1.0.11 的原子保存
  （临时文件 0600 创建、两次替换间失败回滚、`cookie_text` 兜底）。
  新增端点：`POST /api/test/xiaomi/otp/{start,send,verify,restart}`。
- 验证码与风控：单次验证会话 300 秒有效、发送后 60 秒冷却、最多 5 次错误；冷却内的发送请求
  直接返回 `{ok:false, action:"wait", retry_after}`，**不产生任何小米请求**；失败、重试、
  页面刷新都不会自动重发短信，也不会自动重新登录。
- 只跟随 **HTTPS 且主机为 `account.xiaomi.com`** 的 `location`（相对路径补全到该主机），
  其它协议/主机/userinfo/非 443 端口一律拒绝并关闭会话，避免把认证 Cookie 发往第三方主机；
  所有验证请求都不跟随重定向。
- 新增「手动 Cookie 登录」：`POST /api/test/xiaomi/cookie-login`，用 `http.cookies.SimpleCookie`
  解析 `name=value; name=value`。空/无法解析/缺 `userId`/缺 `passToken` 分别返回
  `action:"fix_cookie"` 与具体原因（`userId` 与 `passToken` 都是必需的）；没有 `serviceToken` 时
  用 `MiAccount(session,"","",None)`（预置 `account.token` 的 userId/passToken/deviceId）
  + 经 `_mi_service_login_verified` 在保持 TLS 校验的前提下换取（失败 →
  `action:"exchange_failed"`）；拿到 serviceToken 后**必须**先通过设备列表校验才写盘，
  失败 → `action:"device_list"` 且不写任何文件。Cookie 登录在验证码会话进行中也可以使用，
  成功后清空验证码状态。
- 旧 `/api/test/xiaomi/verify`（粘贴浏览器跳转地址）保留为**高级兼容路径**，并修正归因：
  原来把 HTTP 401 说成「链接可能已过期」，现在如实说明粘贴回来的地址与浏览器会话绑定
  （验证 Cookie 在浏览器里，插件会话里没有），插件通常无法复用，HTTP 401 属于这一类；
  建议改用账号密码或扫码登录。
- 界面重构为三个入口：**扫码登录（推荐）**（原二维码 + 扫码按钮 + 清除登录凭据）、
  **账号密码登录**（登录按钮 + 卡片内可折叠的验证码面板：获取验证码 / 60 秒倒计时 /
  数字验证码输入 / 提交 / 重新开始 / 状态文本，会显示剩余尝试次数并要求重新开始）、
  **高级选项：手动 Cookie 登录**（默认折叠的 `<details>`，含「验证 Cookie」按钮与
  「兼容：粘贴浏览器跳转地址（可能因会话绑定返回 401，不推荐）」子块）。
  移除「免短信」营销标签与「测试」按钮；扫码登录协议、Token 刷新、对话轮询、规则页与
  HAOS Ingress/鉴权均未改动。
- 新端点全部挂在现有 app 对象上、走同一套鉴权中间件；验证码流程在设备列表校验通过前
  **不写任何文件**；进行中的验证码会话不会阻塞扫码登录，也不会阻塞 Cookie 登录。

### 意图规则界面（复选框设备选择、分条保存、规则名称）
- 修复规则页按钮全部失效的问题：复选框/全选/清空等辅助函数把 `querySelectorAll()` 的
  **NodeList** 当成数组调用 `.filter/.map`（浏览器里没有这些方法），导致除「编辑/收起」外的
  按钮（新增、保存、保存全部、删除、导出、测试、设备类型、全选、清空）在真实浏览器里抛
  `TypeError` 而毫无反应；测试桩返回数组，所以 CI 一直绿。已改为 `Array.from(...)`，并把测试桩
  改成与浏览器一致的 NodeList（无 `.filter/.map`），此类回归以后会被 CI 拦住。

- 设备选择从「按住 Ctrl 才能多选」的下拉框改为**复选框列表**：点选即可、显示名称与 `entity_id`、
  顶部「已选择 N 个设备」，支持按名称/`entity_id` 搜索、全选、清空；设备列表异步刷新与 HA
  暂时离线都不会清掉已选实体。
- 每条规则新增**独立「保存」**（编辑/收起 | 保存 | 删除）：只提交当前规则的修改，不会顺带提交
  其他规则未保存的内容；保存失败保留编辑内容并显示原因；顶部保留「保存全部」。规则标题显示
  「未保存」标识，收起/展开与切换页签都不会丢草稿。
- 新增可选的**规则名称**（仅用于展示与管理，不参与语音匹配）。列表标题优先级：规则名称 →
  触发语句 → 「新规则 #N」；原「你说的话」改名为「触发语句（支持匹配表达式）」，底层仍用
  `pattern`，`{num}` 等匹配写法与执行优先级不变。
- 数据兼容：单实体仍保存为字符串、多实体保存为数组、无实体保存为空字符串；旧规则/旧 JSON
  没有 `name` 字段也能正常编辑、保存、导入导出（后端只读 `pattern` 与 `action`，未知字段原样保留）。

### 构建与测试
- 新增 `test_rules_browser.py`（70 项检查，真实 Chromium）：用 Playwright 从固定上游重建页面、
  起本地 HTTP 服务加载真实 HTML、`page.route` 模拟 `/api/config` 与 `/api/devices`，真实点击新增/
  编辑收起/勾选设备/全选/清空/搜索/单条保存/保存全部/删除，并断言 API 载荷（单条保存不夹带其他
  草稿、`entity_id` 单/多/空三种形状）与页面显示；`pageerror`、未处理 Promise、`console.error`
  任一出现即失败。CI 的 host job 会自动安装 Chromium 并运行该套件。
- 新增 `test_xiaomi_login.py`（550 项检查 / 40 个场景，含针对真实 `miservice_fork==2.9.3` 的契约段）：密码登录直接成功、触发验证码、
  发送冷却与「只在显式点击时发送」、同会话 8 步换 Token（含断言所有 identity/verify 请求
  用的是同一个会话对象）、错误验证码计数与耗尽、TTL 过期、重新开始、unsupported 响应、
  `location` 白名单（6 种恶意形式）、Cookie 登录（换取 / 直连 / 过期 / 缺字段 / 探测失败）、
  Cookie 登录与验证码状态互不影响、日志与 JSON 响应不泄露密码/验证码/passToken/serviceToken、
  失败不写 `/data`、前端三个入口与每个按钮实际调用的相对端点（Node + DOM stub）；
  `[real 2.9.3]` 段用官方 sdist 里的真实 `miaccount.py` 钉住两个契约：Cookie 换取依赖
  `account.token` 预置的 userId/passToken/deviceId，以及依赖的 `_serviceLogin` 仍关闭
  TLS 校验（`ssl=False`）而新增流程必须走 `_mi_service_login_verified` 保持校验证书。
- 规则页测试重写为 26 个用例 / 235 条断言（复选框单选/多选、全选/清空/搜索、单条保存与
  草稿隔离、保存失败恢复、规则名称与旧数据兼容、导入导出、设备类型切换），另加真实
  `IntentParser` 行为校验，证明规则名称不参与语音匹配。
- 上述套件已纳入镜像构建期测试与 CI（host job + 镜像内 `docker run`），并加了 6 个变异体检查
  （忽略冷却、绕过 location 白名单、先写凭据后探测设备、容忍缺失 context、Cookie 登录跳过
  设备探测、验证时换新会话），全部被测试杀死。

> ⚠️ **真实小米账号的验证码流程仍未经实机验证**：以上均为隔离环境（假 aiohttp / 假 MiService）
> 下的回归结论。协议本身取自 MiService 2.4+/3.x 源码与 MiIO 抓包注释，但小米线上是否仍按此
> 返回 `flag` / `location` 需要在一台真实 HAOS 上用隔离账号走一遍。失败时界面会给出明确错误
> （`unsupported` / `retry_code` / `restart`），不会假装登录成功；扫码登录始终可用。
> 邮箱验证码（`flag=8`）本版本只做识别，发送接口未实现，会明确返回「暂不支持」。
## 1.0.11

### 登录（短信验证闭环）
- 修复「账号密码登录 → 短信验证」无法完成的问题：1.0.10 的前端从不调用
  `POST /api/test/xiaomi/verify`，验证做完只能靠手工粘贴整段 Cookie。现在「短信验证」
  卡片提供跳转地址输入框与「完成验证」按钮，直接调用该接口并刷新设备列表。
- 短信验证进行中再次点「测试连接」不会再关掉正在等验证的会话（原来会把验证状态丢掉，
  造成「验证做完却一直登不上」的循环）；会原样返回同一条验证链接，15 分钟后过期重建。
- 修复 STS 分支把 `passToken` / `ssecurity` 弄丢的问题：登录状态与 CookieJar 数据分开
  保存、互不覆盖；凭据不完整时不再写入任何文件（原来会写入坏掉的 `cookie_text`）。
- 回调地址严格校验：只接受 `https://api2.mina.mi.com/sts…`，拒绝其它主机、IP、userinfo、
  非 443 端口、非 `/sts` 路径与换行注入；不跟随重定向，避免认证 Cookie 发往其它主机。
- 保存前先校验凭据结构，并用 MiNAService 拉取设备列表确认可用；通过后才写 `auth.json` /
  `.mi.token`：临时文件在创建时就带 `0600` 权限，再依次替换，两次替换之间失败会回滚成
  原来的旧凭据（尽力而为，**不是崩溃安全的完整事务**）。读取旧凭据失败时直接中止写入，
  不会把「读不到」当成「文件不存在」而误删。失败路径不改动任何旧文件。
- `config.yaml` 里的 `cookie_text` 提示在凭据落盘后**单独**写入，仍是上游的普通覆盖写：
  失败只记日志，不影响已保存的凭据（`_get_cookie()` 优先读 `.mi.token`，`cookie_text`
  只是没有令牌文件时的兜底）。
- 「登录成功但没有音箱设备」与「登录失败」分开处理，不再混为一谈。
- 日志只记录回调地址的 host + 路径前缀（不带查询参数与签名），任何令牌都不进日志。

### 构建与测试
- 新增 `test_sms_login.py`：110 项检查 / 12 个场景（触发短信验证、验证链接归一化、
  非法地址拒绝、STS 换取 serviceToken、凭据保存、`passToken`/`ssecurity` 不被 CookieJar
  覆盖、各失败路径不污染 `/data`、会话过期/重复请求/并发、完整 Cookie 登录兼容、
  日志不泄露、前端按钮确实调用 `/verify`）。
- 该套件已纳入镜像构建期测试、CI 与 Release 门禁。

> ⚠️ **真实小米短信验证流程尚未实机验证**：以上均为隔离环境（假 MiService / 假 STS）
> 下的回归结论。真机若遇到小米回调不返回 `serviceToken`，界面会给出明确错误而不是
> 假装登录成功。

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
- **修复对话轮询丢失认证 Cookie**：轮询请求现在显式携带 `userId` + `serviceToken`（保留当前目标音箱
  的 `deviceId`），不再依赖共享 CookieJar 的域名匹配；同时禁止轮询跟随重定向，避免认证 Cookie 被
  发往其它主机。**最小权限**：只发送这三个必要 Cookie，`passToken` / `cUserId` 等登录凭据不会被发往
  对话接口或 mina 域名（扫码登录与 micoapi 刷新仍在 `account.xiaomi.com` 正常使用 `passToken`）。
  根因与修法来自社区 PR #4（@caideone），本版本补充了 CookieJar 修复与回归测试。
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
