# Changelog

## 未发布：登录与凭据修复

- 凭据写入失败后的回滚改为临时文件替换，回滚再次失败时不先删除现存凭据。
- 验证码待验证期间切换账号时关闭旧会话，避免误用上一个账号完成登录。
- 清除登录凭据后同步前端配置版本，避免下次保存被错误判定为冲突。
- 令牌无法写入 `/data` 时说明仅本次运行有效，并正确指向侧边栏面板的“高级设置”页取令牌。
- 换票被小米拒绝（如 `code=70016`）时不再按 10→300 秒无限重试：面板直接写明错误码与
  「请重新扫码登录」，并在等待窗口内盯着凭据文件，用户重新登录后立刻重试。
  网络/STS 抖动仍走原来的指数退避。
- **登录态主动续期（本版重点）**：桥接器不再等 401 才救——`serviceToken` 还在有效期内
  就每 6 小时（`token_refresh_hours` 可调）用保存的凭据换一张新的，并把签发时间写进
  `.mi.token`。每次换票都会拿到新的 `passToken`，所以只要续期成功，登录态就不会因为
  自然过期而中断，用户不需要反复扫码。
  续期失败时**不显示红色横幅**（当前 token 还能用），只在日志记一条并按 5 分钟重试；
  只有轮询真的 401 且换票被拒时，才提示重新登录。
- Cookie 登录用粘贴的 `serviceToken` 兜底时，明确告知保存的 `passToken` 未经校验、
  浏览器 token 到期后需要重新登录（此前要到几天后才在面板上看到「登录已过期」）。
- **日志可追溯**：磁盘日志 `logs/bridge.log` 改为轮转（2 MB × 4 份），长期运行不会写满
  SD 卡；面板「实时日志」改为读磁盘日志尾部，单页从 80 行提到 500 行——判断「登录态
  续期是否每天都成功」需要回看数天，原来的 80 行只有几分钟。
- **监听全部小爱设备（issue #15）**：此前只轮询 `devices[0]`，账号里有多台小爱设备时，
  对其它音箱说话桥接器完全收不到（表现为「只有带屏幕那台能用」），而文档却写着「规则对
  所有设备生效」。现在默认轮流轮询全部小爱设备（每台各自一份 `last_timestamp`），可用
  `xiaomi.device_ids` 限定范围；`device_id` / `device_did` 仍表示「只听这一台」。
  文档同时说明：设备列表里只有小爱设备，米家 IoT 设备由 Home Assistant 实体负责。

## 1.0.16：验证码登录继续修复

### 修复
- 修复 1.0.15 之后仍出现的「小米没有完成本次登录（缺少 ssecurity、nonce）」：
  小米在验证码通过后只返回一个跳转地址时，插件现在会按该地址补走一步再完成登录，不再直接失败。

### 改进
- 手动 Cookie 登录改成两个输入框（userId 与 passToken 各一个），不用再自己拼「userId=…; passToken=…」；
  粘贴带键名的内容也能识别。

## 1.0.15：短信验证码登录修复

### 修复
- 修复部分账号短信验证码登录失败：验证码提交成功后仍提示「登录响应缺少必要字段」，只能改用扫码登录。
- 小米在登录过程中要求再验证一次时，会提示重新获取验证码继续登录，不再直接判定失败。
- 小米反复要求验证时会停止本次登录并提示改用扫码登录，不会连续发送短信。
- 登录失败时说明具体原因，并记录便于排查问题的信息。
- 修复个别情况下密码校验可能被跳过的问题。

### 其他
- 本版没有新增功能，只修复登录。

## 1.0.14：真机登录修复 + 定时任务表单

### 登录
- 修复真机短信验证码登录「验证码已通过，但恢复登录失败」的问题。
- 手动 Cookie 登录改为先换取令牌、换取失败才用粘贴的令牌兜底；界面上也同步调整了复制说明。

### 定时任务
- 定时任务页新增「新建定时任务」表单：时间、重复（每天 / 工作日 / 周末 / 自定义星期）、
  动作与目标实体，创建结果与语音创建的任务完全一致。
- 表单会先校验时间、星期、服务与实体；创建失败或结果无法确认时给出明确提示，
  HA 已保存但重新加载失败时提示手动重载，不再显示成「创建失败」。

## 1.0.13

本版把 1.0.12 之后的规则页修复一起发出去，并完成一轮安全与可靠性加固。

### 安全
- 直连登录改为每次签发随机会话，退出登录即在服务端撤销；加载项重启后直连浏览器需要重新登录，
  面板入口不受影响。
- 所有管理接口统一在服务端鉴权，配置里的密码、令牌与 Cookie 一律脱敏返回。
- 状态变更请求校验来源，会话 Cookie 加上 HttpOnly 与 SameSite；关闭接口文档页，
  扫码登录与对话轮询恢复 TLS 证书校验。
- 文档明确直连端口是明文 HTTP：建议只在可信局域网使用，优先走面板入口。

### 配置保存
- 分页保存加上版本校验：页面在其他窗口被改过时提示冲突，不再覆盖对方数据。
- 只提交部分字段时，不再删除未提交的配置。

### 登录与凭据
- 凭据文件统一用私有权限原子替换，写入失败回滚；两份凭据不一致时拒绝混合登录并提示重新认证。
- 清除登录失败时如实报错，不再谎报「已清除」。

### 语音与定时
- 语音去重记录损坏时停止桥接，而不是重放旧语音；延迟任务不跨重启，
  需要断电后仍执行请用 Home Assistant 的定时自动化。
- 定时规则支持带秒的时间与更多星期写法，非法值会被拒绝而不是静默失败。

### 规则页
- 保存期间继续编辑不再丢失，「保存全部」也不再覆盖期间的新修改。
- 「其他(JSON)」无法解析、或试图覆盖关键字段时拒绝保存并提示原因。

## 1.0.12

### 新增：账号密码 + 短信验证码登录
- 新增账号密码登录：小米不要求二次验证时直接完成登录并保存凭据。
- 需要验证码时在页面里完成「获取验证码 → 填验证码 → 完成登录」，只有点「获取验证码」才会发短信；
  验证码 5 分钟内有效、发送后 60 秒冷却、最多 5 次错误。
- 登录过程中只跟随小米本站的跳转地址，避免登录凭据被发往其他主机。

### 新增：手动 Cookie 登录
- 高级选项里可以直接粘贴 Cookie 登录（需要登录凭据），登录后先确认能读到音箱设备才保存。
- 旧的「粘贴浏览器跳转地址」保留为兼容入口，并说明它通常因会话绑定而返回 401，
  建议改用密码登录或扫码登录。

### 界面
- 小米登录页整理为三个入口：扫码登录（推荐）、账号密码登录、高级选项（手动 Cookie 登录）。

### 意图规则页
- 修复规则页按钮在真实浏览器里全部失效的问题（新增、保存、删除、导出等点击无反应）。
- 设备选择改为复选框列表：可搜索、全选、清空，并显示已选数量。
- 每条规则可以单独保存，顶部保留「保存全部」；规则支持可选名称，切换页面不会丢失未保存的修改。

## 1.0.11

### 修复：短信验证做不完
- 短信验证卡片新增「完成验证」按钮，验证做完直接完成登录，不再需要手动复制整段 Cookie。
- 验证过程中再点「测试连接」不会再把验证会话丢掉，不会再出现「验证做完却一直登不上」。
- 回调地址只接受小米官方地址；凭据不完整或校验不通过时不写任何文件。
- 修复换取令牌后登录凭据丢失、需要反复登录的问题。
- 「登录成功但没有音箱设备」与「登录失败」分开提示。

## 1.0.10

### 安全
- 管理界面改为 Home Assistant 面板入口，沿用 HA 登录；直连端口仍然可用，但需要单独的访问令牌。
- 所有管理接口在服务端统一鉴权，配置里的密码、令牌与 Cookie 脱敏返回。
- 关闭接口文档页，恢复 TLS 证书校验，日志不再输出令牌。

### 登录稳定性
- 令牌过期后统一走一次恢复流程，失败时逐次拉长重试间隔，不再按轮询频率反复重试。
- 修复对话轮询丢失认证信息导致收不到语音的问题，并避免把登录凭据发往对话接口。
- 新增「清除登录凭据」按钮：清除后不会再被旧页面自动恢复。

### 规则编辑
- 设备列表异步加载不再覆盖未保存的规则修改，并提示「有未保存修改」。
- 保存失败时显示具体原因，不再伪装成保存成功。

### 构建与发布
- 固定运行期依赖版本；构建时自动跑回归测试，任一失败即拒绝构建。
- 测试与发布拆成两个流程：测试只验证不发布，发布只在主分支手动触发且只推版本标签。

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
