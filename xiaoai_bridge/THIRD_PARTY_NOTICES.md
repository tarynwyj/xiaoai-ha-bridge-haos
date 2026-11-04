# 第三方代码与许可说明

## 授权范围

本仓库中由 tarynwyj 拥有版权并有权许可的原创 HAOS 适配、补丁、测试、构建配置与文档
采用自定义的「XiaoAI HAOS 非商业使用许可协议 1.0」，全文见根目录 LICENSE。
允许免费非商业使用和免费非商业再分发；商业使用及销售须事先取得书面授权，
授权费或销售分成另行签约。本协议含商业限制，属于源码可见许可，不是 OSI 定义的开源许可证。

该版权及许可声明仅适用于维护者有权许可的原创贡献。
上游代码、其他贡献者的贡献及第三方依赖的版权归各自权利人所有，
不因根目录 LICENSE 而转移，也不替代其已有许可条款。
此前已按有效许可取得的权利不因本次调整而追溯撤销，包括此前 MIT 版本的许可。

## xiaoai-ha-bridge

- 上游项目：https://github.com/chenshuhe/xiaoai-ha-bridge
- 构建使用提交：`e4c82d4b4e872ff5d02d1baec089a6b4ad36734a`
- 许可来源：该固定提交的 [README「许可证」章节](https://github.com/chenshuhe/xiaoai-ha-bridge/blob/e4c82d4b4e872ff5d02d1baec089a6b4ad36734a/README.md#许可证)
  明确声明 `MIT License`。
- 使用方式：Docker 构建时获取上游代码，由本仓库的补丁脚本修改
  `bridge.py` 和 `web/index.html`，再作为 HAOS 加载项运行。

该固定提交没有独立的 LICENSE 文件，也未在 README 的许可章节列出版权姓名与年份。
本说明按原样记录其 MIT 声明，不代上游补写版权信息；上游已有的声明与署名应予保留。
本仓库根目录 LICENSE 中的 tarynwyj 版权行不适用于上游代码。

镜像保留上游 README，并额外将其原样复制到
`/usr/share/licenses/xiaoai-ha-bridge/README.md`；
本仓库的 LICENSE 与本说明放在
`/usr/share/licenses/xiaoai-ha-bridge-haos/`。
升级上游固定提交时，应重新核对许可声明与版权信息，并同步更新本说明。

## 第三方依赖

运行依赖列于 `xiaoai_bridge/requirements.lock.txt`，包括
`miservice_fork`、`aiohttp`、`FastAPI` 等；
传递依赖与基础镜像中的软件也遵循各自的许可证。
本说明不是整个容器的完整依赖许可证清单，根目录 LICENSE 不替代这些许可证。
再分发时应保留各依赖随包提供的版权及许可文件，并遵守其条款。

## 构建副本

`xiaoai_bridge/LICENSE` 和 `xiaoai_bridge/THIRD_PARTY_NOTICES.md` 是根目录同名文件的
构建副本，便于以 `xiaoai_bridge/` 为 Docker 构建上下文时随镜像分发。
CI 与发布流程会检查两组文件完全一致；修改时须同步更新。
