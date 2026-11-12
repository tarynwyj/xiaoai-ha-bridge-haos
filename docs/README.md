# 文档目录

这里的文档按「你想做成什么事」来分。不确定从哪看，照下表选一篇就行。

| 你的情况 | 打开 |
|---|---|
| 还没装，想知道能不能用、怎么装 | [安装与首次配置](install.md) |
| 装好了，想加语音规则、定时任务，或管多台音箱 | [使用指南](usage.md) |
| 登录小米失败、总提示要重新登录、遇到 `70016` | [登录小米账号](login.md) |
| 某个环节不对，想按症状排查 | [故障排查](troubleshooting.md) |
| 想知道数据存在哪、会不会外传 | [数据与隐私](data-and-privacy.md) |
| 要升级、回退，或想确认版本 | [升级与回退](upgrade.md) |

## 这份文档的约定

- 面板指 Home Assistant 左侧边栏的 **小爱语音桥接** 页面，共八页：仪表盘、实时日志、
  小米账号、Home Assistant、意图规则、设备管理、定时任务、高级设置。
- 「实体 ID」是 Home Assistant 里设备的唯一名字，例如 `light.ke_ting_deng`；
  在面板里用复选框选设备时不需要手写它。
- 「自动化」指写进 Home Assistant 的定时任务，名字以 `xiaoai_bridge_` 开头。
- 文档里的界面文字用「」标出，和面板上看到的保持一致。
- 截图经过裁剪和遮挡，里面的状态、数量只代表拍摄那一刻。

## 相关文件

- 项目主页与安装入口：[README](../README.md)
- 各版本改了什么：[CHANGELOG](../xiaoai_bridge/CHANGELOG.md)
- 加载项页面里的「文档」页：[DOCS.md](../xiaoai_bridge/DOCS.md)
- 想改代码、跑测试、发版：[DEVELOPMENT.md](../DEVELOPMENT.md)
