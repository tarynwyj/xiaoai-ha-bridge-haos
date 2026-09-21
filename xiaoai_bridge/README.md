# XiaoAI HA Bridge

Bridge XiaoAI voice commands to Home Assistant services.

Open the web UI after installation to configure:
- Xiaomi account and speaker
- Home Assistant URL and Long-Lived Access Token
- Voice intent rules
- Device aliases and schedules

This build adds an HA entity drop-down to intent rules. The list is loaded from the existing `/api/devices` endpoint and filtered by the selected domain.
