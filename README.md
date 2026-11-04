# XiaoAI HA Bridge

**让小爱同学控制 Home Assistant 里的智能家居设备。**

对着小爱音箱说「打开客厅灯」，家里的灯就亮了。支持自定义语音指令、多设备联动、
设备别名和定时任务，全部在 Home Assistant 侧边栏里配置，不用写 YAML。

[![CI](https://github.com/tarynwyj/xiaoai-ha-bridge-haos/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/tarynwyj/xiaoai-ha-bridge-haos/actions/workflows/ci.yml)

> 当前版本 **1.0.16**，变更记录见 [CHANGELOG](xiaoai_bridge/CHANGELOG.md)。

---

## 功能亮点

- 🎤 **语音控制**：小爱同学的语音指令直接触发 Home Assistant 服务，支持灯、开关、空调、
  场景、脚本、媒体播放器、自动化等设备类型。
- 🔗 **意图规则自由配置**：一条规则 = 一句话 + 一个动作 + 若干目标实体；支持正则匹配、
  数字提取（「把空调调到 26 度」）和自定义语音回复。
- 🧩 **多设备联动**：一条规则可以同时控制多个实体（例如「关灯」一次关掉客厅、餐厅、走廊）。
- 🕐 **定时任务**：说「晚上 11 点关灯」，会在 Home Assistant 中创建对应的定时自动化。
- 📱 **Home Assistant 侧边栏管理**：面板通过 HAOS Ingress 打开，沿用 Home Assistant 登录，
  不需要记额外密码。
- 🔐 **本地保存、按需外发**：配置与登录态保存在 `/data`，界面与配置接口会脱敏；运行时会
  按需向小米服务端、你的 Home Assistant、以及你主动启用的 AI 服务发起必要请求
  （详见[安全与数据流向](#安全与数据流向)）。
- ♻️ **登录态自动恢复**：Token 过期后自动用保存的凭据换取新 Token，失败按指数退避重试，
  不会擦除仍然有效的凭据。

## 界面预览

| 页面 | 用途 |
|---|---|
| 仪表盘 | 启动/停止桥接器、运行状态、指令执行数、最近一条语音 |
| 实时日志 | 运行日志（最近 80 条，可自动滚动） |
| 小米账号 | 三个入口：扫码登录（推荐）/ 账号密码 + 验证码（1.0.12 重写，待实机验证）/ 高级：手动 Cookie 登录；含清除登录凭据 |
| Home Assistant | HA 地址与长期访问令牌 |
| 意图规则 | 语音 → 动作 → 设备映射，支持搜索、导入导出、在线测试 |
| 设备管理 | 设备别名与设备列表 |
| 定时任务 | 由语音创建的自动化列表 |
| 高级设置 | 轮询间隔、AI 托管、显示直连令牌 |

> 界面截图待补充（管理面板 / 规则编辑 / 设备列表）。欢迎提 issue 或 PR 提供截图。

## 快速安装

1. **添加仓库**：Home Assistant → 设置 → 加载项 → 右上角 ⋮ → 仓库 →
   粘贴 `https://github.com/tarynwyj/xiaoai-ha-bridge-haos` → 添加。
2. **安装**：在加载项商店里找到 **XiaoAI HA Bridge** → 安装 → 启动。
3. **绑定小米账号**：打开左侧边栏的 **小爱语音桥接** 面板 → 小米账号 → 用米家 App 扫码登录。
4. **连接 Home Assistant**：Home Assistant 页填地址（如 `http://homeassistant.local:8123`）
   和长期访问令牌，点「测试」。
5. **配置规则**：意图规则页 → 新增 → 填「规则名称」（可选）与「触发语句」、勾选设备、
   点该条规则的「保存」→ 回仪表盘点「启动」。
6. **试一句**：对小爱说「打开客厅灯」，看仪表盘是否出现执行记录。

> 需要 Home Assistant OS（Supervisor 加载项）、amd64 架构、米家账号和至少一台小爱音箱。
> 详细步骤与常见问题见 [配置文档](xiaoai_bridge/DOCS.md)。

## 使用示例

在「意图规则」里加一条：

| 字段 | 填写 |
|---|---|
| 规则名称（可选） | `客厅灯组开灯` |
| 触发语句 | `打开客厅灯` |
| 设备类型 | 灯光 |
| 操作 | 开 |
| 实体 ID | `light.ke_ting_deng`（复选框，可多选） |
| 回复 | `好的` |

保存后对小爱说「打开客厅灯」，灯会开，小爱回「好的」。

几个进阶写法：

- **带数字的参数**：语句写 `把空调调到{num}度`，动作的「温度」填 `{0}`，
  说「把空调调到 26 度」就会调用 `climate.set_temperature`，temperature=26。
- **多实体**：在实体复选框里勾选多台设备即可，一条规则同时控制多个设备；列表支持搜索、
  全选、清空，收起再展开不会丢失勾选。
- **分条保存**：每条规则右侧有「保存」，只提交这一条；顶部「保存全部」才统一提交所有修改，
  未保存的规则标题会显示「未保存」。
- **定时**：在「其他(JSON)」里填
  `{"type":"schedule","schedule_time":"23:00:00","schedule_days":[1,2,3,4,5,6,7]}`，
  说对应语句时会在 Home Assistant 里创建一条 `xiaoai_bridge_*` 自动化。
- **先试后说**：规则页顶部的测试区可以输入一句话，直接看匹配到哪条规则、会执行什么。

## 常见问题

- **侧边栏看不到面板？** 确认加载项已启动，并在 设置 → 加载项 → XiaoAI HA Bridge 中
  打开「显示在侧边栏」；需要 Home Assistant 管理员账号。
- **说话没反应？** 先看仪表盘有没有「最近」语音记录：没有 → 小米登录态失效；
  有记录但没执行 → 规则未匹配或实体 ID 不对（用规则页测试区验证）；细节看实时日志。
- **直连端口打不开？** 1.0.10 起 `http://<主机IP>:47521` 需要直连令牌，在面板「高级设置」页查看。
- **一定要扫码吗？** 不一定。1.0.12 起小米账号页有三个入口：**扫码登录（推荐）**、
  **账号密码登录**（小米要求二次验证时，验证码直接在该卡片里提交）、**高级选项：手动 Cookie 登录**
  （默认折叠）。账号密码与 Cookie 流程已通过模拟回归测试，**真实小米账号兼容性仍待验证**，
  卡住时请优先用扫码登录。
- **账号密码 / 验证码怎么用？** 填账号密码 → 点「登录」；若小米要求二次验证，卡片里会出现
  验证码面板 → 点「获取验证码」（60 秒冷却）→ 输入验证码 → 点「提交验证码」。
  验证码错误会显示剩余次数，连续错误或超时（5 分钟）请点「重新开始」再登录一次。
  拿不到有效凭据时不会写入任何文件，扫码登录不受影响。
- **Cookie 登录怎么用？** 展开「高级选项：手动 Cookie 登录」→ 粘贴包含 `userId` 与 `passToken`
  的浏览器 Cookie → 点「验证 Cookie」。旧版「粘贴浏览器跳转地址」保留在同一张卡片的
  「兼容」子块里，但它与浏览器会话绑定，**通常无法复用并会返回 401**，不建议作为常规方式。
- 更多问题见 [完整配置文档与 FAQ](xiaoai_bridge/DOCS.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [xiaoai_bridge/DOCS.md](xiaoai_bridge/DOCS.md) | 完整配置、FAQ、升级与备份恢复 |
| [xiaoai_bridge/CHANGELOG.md](xiaoai_bridge/CHANGELOG.md) | 各版本新增功能与修复 |
| [DEVELOPMENT.md](DEVELOPMENT.md) | 补丁机制、构建、测试、CI 与发布流程 |

## 安全与数据流向

本加载项**不把数据上传到作者或第三方服务器**，但运行中必然会与以下服务通信：

| 数据 | 存在哪里 / 发往哪里 | 说明 |
|---|---|---|
| 配置（规则、别名、HA 地址、轮询间隔） | 留在 `/data/config/config.yaml` | 不对外发送 |
| 小米登录态（`passToken`、`serviceToken` 等） | 留在 `/data/config/`；**会**发往小米服务端 | 用于登录、刷新 Token、拉取设备与对话记录；这是使用小爱音箱的必要条件 |
| Home Assistant 长期访问令牌 | 留在 `/data/config/`；**会**发往你填写的 HA 地址 | 用于调用 HA 服务与读取实体 |
| 语音文本 | 发往小米（对话记录由小米提供）与本机；若你**主动启用**「AI 托管」，还会发给你配置的第三方 AI 接口 | 默认关闭，仅在开启后生效 |
| 直连令牌 | 留在 `/data/config/.access_token` | 只用于直连端口鉴权 |

使用本加载项时，请遵守小米账号及相关服务条款。

安全要求：

- 面板走 HAOS Ingress，只有 Home Assistant 管理员能打开；直连端口 47521 需要访问令牌，
  且是明文 HTTP，**只建议在可信局域网内使用**，跨网段或公网请走侧边栏面板；
- 界面对 `cookie_text`、密码、HA Token、AI Key 一律脱敏，`/api/config` 也不会返回它们；
- 日志不打印直连令牌与小米令牌，只提示令牌文件位置；
- 凭据保存在 `/data`，会随 Home Assistant 备份一起备份 —— **备份文件里包含这些凭据，
  请自行妥善保管，不要公开分享**；怀疑泄露时清除登录凭据并重建直连令牌。
- 请勿把本加载项暴露到公网，也不要公开贴出包含 Cookie / 令牌的日志。

## 与上游项目的关系

本项目是 [chenshuhe/xiaoai-ha-bridge](https://github.com/chenshuhe/xiaoai-ha-bridge) 的
Home Assistant OS 适配与增强版：构建时拉取固定 commit 的上游代码，再由本仓库的补丁脚本
加上鉴权、界面与稳定性修复。核心桥接逻辑的功劳属于上游作者。

## 鸣谢

- [chenshuhe/xiaoai-ha-bridge](https://github.com/chenshuhe/xiaoai-ha-bridge) —— 上游桥接器；
- [MiService](https://github.com/Yonsm/MiService) —— 小米账号与设备接口；
- **[@caideone](https://github.com/caideone)** —— 定位并修复了小爱对话轮询丢失认证 Cookie 的
  问题（PR #4），这正是「显示已连接但语音指令数为 0」的根因；
- 以及所有提 issue、帮忙在真机上验证的用户。

## License

本仓库中由维护者拥有版权的原创 HAOS 适配、补丁脚本、测试、构建配置与文档采用
MIT License，详见 [LICENSE](LICENSE)。

本项目基于 [chenshuhe/xiaoai-ha-bridge](https://github.com/chenshuhe/xiaoai-ha-bridge)，
其构建所用固定提交的 README 声明采用 MIT License。
上游来源、固定提交、授权范围及第三方依赖说明见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)；第三方代码遵循各自许可证。

---

<details>
<summary>English summary</summary>

**XiaoAI HA Bridge** — control Home Assistant devices with natural language through a
Xiaomi XiaoAI smart speaker. It runs as a Home Assistant OS add-on, opens from the sidebar
(HAOS Ingress), and maps spoken phrases to Home Assistant service calls with a visual rule
editor (multi-entity actions, aliases, scheduled automations, TTS replies).

Install: add `https://github.com/tarynwyj/xiaoai-ha-bridge-haos` as an add-on repository,
install **XiaoAI HA Bridge**, open the sidebar panel, log in to Xiaomi by QR code, fill in
your Home Assistant URL + long-lived token, then add intent rules and start the bridge.

Developer docs (patching, tests, CI, release): [DEVELOPMENT.md](DEVELOPMENT.md).
User docs (Chinese): [xiaoai_bridge/DOCS.md](xiaoai_bridge/DOCS.md).

</details>
