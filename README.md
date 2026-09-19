# XiaoAI HA Bridge for Home Assistant OS

这是一个给 Home Assistant OS 使用的预构建镜像包装，实际桥接程序来自：

- https://github.com/chenshuhe/xiaoai-ha-bridge

目标是避免在 HAOS 本机执行 Docker 构建，从而绕开 Docker Hub builder 镜像下载失败的问题。

## GHCR 镜像

```
ghcr.io/tarynwyj/xiaoai-ha-bridge-haos:1.0.0
```

首次构建后，请在 GitHub 的 Packages 页面把容器包可见性改成 **Public**，这样 HAOS 可以匿名拉取。

## HAOS 本地 App 配置

把仓库里的 `haos-config.yaml` 内容覆盖到：

```
/addons/xiaoai_bridge/config.yaml
```

刷新应用商店后重新安装即可。

## 持久化

上游程序原本把配置写入 `config/`、日志写入 `logs/`。本镜像将它们映射到 Home Assistant App 的持久化 `/data`：

- `/data/config`
- `/data/logs`

因此重新创建容器后配置仍会保留。
