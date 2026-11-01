# XiaoAI HA Bridge

**让小爱同学控制 Home Assistant 里的智能家居设备。**

对着小爱音箱说「打开客厅灯」，家里的灯就亮了。支持自定义语音指令、多设备联动、
设备别名和定时任务，全部在 Home Assistant 侧边栏里配置，不用写 YAML。

[![CI](https://github.com/tarynwyj/xiaoai-ha-bridge-haos/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/tarynwyj/xiaoai-ha-bridge-haos/actions/workflows/ci.yml)

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
- 🔐 **凭据不出本机**：小米登录态与 Home Assistant Token 只保存在 `/data`，日志不打印令牌。
- ♻️ **登录态自动恢复**：Token 过期后自动用保存的凭据换取新 Token，失败按指数退避重试，
  不会擦除仍然有效的凭据。

## 界面预览

| 页面 | 用途 |
|---|---|
| 仪表盘 | 启动/停止桥接器、运行状态、指令执行数、最近一条语音 |
| 实时日志 | 运行日志（最近 80 条，可自动滚动） |
| 小米账号 | 米家 App 扫码登录 / Cookie 登录 / 清除登录凭据 |
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
5. **配置规则**：意图规则页 → 新增 → 填「你说的话」、选设备、保存 → 回仪表盘点「启动」。
6. **试一句**：对小爱说「打开客厅灯」，看仪表盘是否出现执行记录。

> 需要 Home Assistant OS（Supervisor 加载项）、amd64 架构、米家账号和至少一台小爱音箱。
> 详细步骤与常见问题见 [配置文档](xiaoai_bridge/DOCS.md)。

## 使用示例

在「意图规则」里加一条：

| 字段 | 填写 |
|---|---|
| 你说的话 | `打开客厅灯` |
| 设备类型 | 灯光 |
| 操作 | 开 |
| 实体 ID | `light.ke_ting_deng`（按住 Ctrl 可多选） |
| 回复 | `好的` |

保存后对小爱说「打开客厅灯」，灯会开，小爱回「好的」。

几个进阶写法：

- **带数字的参数**：语句写 `把空调调到{num}度`，动作的「温度」填 `{0}`，
  说「把空调调到 26 度」就会调用 `climate.set_temperature`，temperature=26。
- **多实体**：实体 ID 多选即可，一条规则同时控制多个设备。
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
- **一定要扫码吗？** 也可以用浏览器 Cookie 登录。账号密码 + 短信验证流程目前**尚未闭环**，
  暂不推荐。
- 更多问题见 [完整配置文档与 FAQ](xiaoai_bridge/DOCS.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [xiaoai_bridge/DOCS.md](xiaoai_bridge/DOCS.md) | 完整配置、FAQ、升级与备份恢复 |
| [xiaoai_bridge/CHANGELOG.md](xiaoai_bridge/CHANGELOG.md) | 各版本新增功能与修复 |
| [DEVELOPMENT.md](DEVELOPMENT.md) | 补丁机制、构建、测试、CI 与发布流程 |

## 安全说明

- 面板走 HAOS Ingress，只有 Home Assistant 管理员能打开；
- 直连端口 47521 需要访问令牌（`/data/config/.access_token`），且是明文 HTTP，
  **只建议在可信局域网内使用**；跨网段或公网请走侧边栏面板；
- `/api/config` 不会返回小米 Cookie、密码或 Home Assistant Token；
- 附加组件日志不会打印直连令牌，只提示令牌文件位置。

## 与上游项目的关系

本项目是 [chenshuhe/xiaoai-ha-bridge](https://github.com/chenshuhe/xiaoai-ha-bridge) 的
Home Assistant OS 加载项封装：构建时拉取固定 commit 的上游代码，再由本仓库的补丁脚本
加上鉴权、界面与稳定性修复。核心桥接逻辑的功劳属于上游作者。

## 鸣谢

- [chenshuhe/xiaoai-ha-bridge](https://github.com/chenshuhe/xiaoai-ha-bridge) —— 上游桥接器；
- [MiService](https://github.com/Yonsm/MiService) —— 小米账号与设备接口；
- **[@caideone](https://github.com/caideone)** —— 定位并修复了小爱对话轮询丢失认证 Cookie 的
  问题（PR #4），这正是「显示已连接但语音指令数为 0」的根因；
- 以及所有提 issue、帮忙在真机上验证的用户。

## License

本仓库只包含加载项封装、补丁脚本与测试；上游桥接器代码按其自身许可证分发。
请遵守小米账号服务条款，不要把本加载项暴露到公网。

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
