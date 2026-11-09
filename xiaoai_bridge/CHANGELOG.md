# Changelog

## 1.0.17：登录续期、定时任务与多设备监听

### 登录与凭据
- 新增**登录态主动续期**：`serviceToken` 到期前每 6 小时自动换新（`token_refresh_hours` 可调），
  登录态不再因为自然过期中断，不必反复扫码；续期失败不弹红色提示，只按 5 分钟重试。
- 换票与保存分开报告：换票成功但写盘失败时如实说明「已换取新 Token，但未能写入 `/data`」，
  下次只重试保存，不会为保存失败再向小米换一次票。
- 换票被小米拒绝（如 `code=70016`）时不再按 10→300 秒无限重试，直接写明错误码并提示重新扫码；
  网络或 STS 抖动仍按原退避重试。
- 拒绝跨账号写入：换票返回的账号与已保存的不一致时停止并提示；`passToken` 缺失时保留原值，不再清空。
- 登录设备标识改为稳定复用，失败重试不再每次换一个「新设备」；扫码/Cookie 用户的换票路径不再
  携带账号密码，不会再被密码回退打成 87001。

### 定时任务
- 新增**面板表单**：选设备、选操作、选时间和重复方式，不用写 domain/service/JSON；
  动作选项跟随 HA 实际提供的服务，创建后回读 HA，区分「已生效 / 已提交但未加载 / 无法确认」。
- 新增**中文语音建任务**：「每天早上七点半打开热水器」「每周一三五晚上九点关掉客厅灯」
  「明天八点」「十分钟后」「三十秒钟后」都能识别；同一句话不会重复建。
- 新增**一次性任务**：指定日期或相对时间，按「到点触发 + 日期守卫」只执行一次；
  执行后用「清理已完成任务」移除，HA 没执行的过期任务保留并标「已过期未执行」。
- 支持**按房间筛选设备**、**编辑**（覆盖同一条自动化，不会新建第二条）、**暂停/启用**（按真实实体）。
- 列表显示下次执行时间、来源（语音/面板）与「HA 未加载」标记；列表本身只读，刷新不会删除任务。

### 设备监听
- 新增**监听范围**：可选「监听全部小爱音箱」或「仅监听所选」，多台账号可以只挑几台；
  「仅监听所选」一台都不选时保存会被拒绝，不会退化成监听全部。
- 修复多台小爱只监听第一台的问题（感谢 @Yale-Yip 在 issue #15 中反馈）：默认轮流轮询全部小爱设备，每台各自去重；
  旧版单台 `device_id` / `device_did` 仍表示「只听这一台」，保存后自动迁移到新字段。
- 设置了范围但当前一台都没发现时等待上线，不再回退去听没有勾选的音箱。

### 修复
- 已有即时规则不再抢先执行定时语音：保留「明天 / 十分钟后 / 每天」等时间语义与规则里配置的设备、
  动作参数；时间解析失败时不会立即操作设备。
- 修复「晚上十二点」被算成中午 12 点、「半分钟后」被算成 30 分钟。
- 修复语音说「三十秒钟后」约一半会失败：相对时间保留秒数，触发时间、登记表、下次执行与
  编辑回填保持一致，并兼容旧版只记分钟的记录。
- 修复多实体规则建定时任务时把目标写成 Python 列表字符串（HA 无法执行）。
- 修复 HA 连不上时面板把任务全标成「HA 未加载」、暂停/启用误报「HA 里没有这个任务」。
- 修复任务列表接口报错时红条只显示 `undefined`。
- 修复登记表损坏后新建任务会冲掉其它任务记录：读取失败时停止新建/编辑/删除/清理并保留原文件，
  登记表改为临时文件 + fsync + 原子替换；HA 已删除但本地登记未清理时提示部分完成。

### 其他
- 日志改为轮转（2 MB × 4 份），面板「实时日志」改读磁盘尾部，单页 80 行提到 500 行。
- 补充登录续期、定时任务、设备监听与浏览器交互的回归测试，并新增 HTTP 级 HA 契约测试。

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
