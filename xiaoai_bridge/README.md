# XiaoAI HA Bridge

Bridge XiaoAI voice commands to Home Assistant services.

Open the web UI after installation to configure:
- Xiaomi account and speaker
- Home Assistant URL and Long-Lived Access Token
- Voice intent rules
- Device aliases and schedules

This build adds an HA entity selector to intent rules. The list is loaded from the existing `/api/devices` endpoint and filtered by the selected domain. Hold Ctrl to select several entities for one action; saved rules apply to the next voice command without restarting the bridge.

The rules page shows compact summaries by default. Search by spoken phrase, entity ID, or device name, then click Edit on a rule to inspect or change its details.

QR and Cookie logins exchange the Xiaomi account `passToken` for a fresh `micoapi` service token. A login is only saved after Xiaomi returns a non-empty service token.
