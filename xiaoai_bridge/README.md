# XiaoAI HA Bridge

让小爱音箱控制 Home Assistant 里的设备：说「打开客厅灯」，灯就亮。

- 语音规则可自己配：一句话 → 一个动作 → 多台设备；
- 说「明天八点打开热水器」就能建定时任务，任务写进 Home Assistant，不依赖加载项常驻；
- 账号下有多台小爱音箱时默认全部监听，也可以只选其中几台；
- 管理面板在 Home Assistant 侧边栏，沿用 HA 登录，不用额外密码。

## 安装后三件事

1. **小米账号**页扫码登录；
2. **Home Assistant** 页填地址和长期访问令牌，点「测试」；
3. **意图规则**页建一条规则并保存，回仪表盘点「启动」。

然后对小爱说出规则里的语句即可。

## 需要

Home Assistant OS（带 Supervisor）、`amd64` 架构、小米账号、至少一台小爱音箱。

## 已知限制

- 账号密码 + 验证码流程只通过模拟回归测试，真实账号尚未验证；扫码最稳；
- 邮箱验证码（小米返回 `flag=8`）只识别、不发送，会提示改用扫码；
- 直连端口 `47521` 是明文 HTTP，只在可信局域网使用；
- 米家的灯、插座、空调不由本加载项管理，它们归 Home Assistant 的对应集成。

## 更多

- 详细说明：加载项页面的「文档」页，或
  [仓库文档目录](https://github.com/tarynwyj/xiaoai-ha-bridge-haos/blob/main/docs/README.md)
- 版本变更：[CHANGELOG](CHANGELOG.md)
- 项目主页与开发说明：[README](../README.md)、[DEVELOPMENT](../DEVELOPMENT.md)

核心桥接逻辑来自 [chenshuhe/xiaoai-ha-bridge](https://github.com/chenshuhe/xiaoai-ha-bridge)。
