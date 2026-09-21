# XiaoAI HA Bridge for Home Assistant OS

Home Assistant App wrapper for [chenshuhe/xiaoai-ha-bridge](https://github.com/chenshuhe/xiaoai-ha-bridge).

## Install

Add this repository to the Home Assistant App Store:

`https://github.com/tarynwyj/xiaoai-ha-bridge-haos`

Then install **XiaoAI HA Bridge** from that repository.

## Image

The app uses the prebuilt image:

`ghcr.io/tarynwyj/xiaoai-ha-bridge-haos:<version>`

The version in `xiaoai_bridge/config.yaml` is the single source of truth for both the App Store metadata and the image tag.

## Local development

The Docker image pins a known upstream XiaoAI HA Bridge commit and applies the authentication and web UI patches in `xiaoai_bridge/` during build. CI compiles both patches, builds the image, verifies the Xiaomi `micoapi` token exchange and entity selector patches are present, verifies the Home Assistant version label, and only then pushes the image.
