# XiaoAI HA Bridge

Bridge XiaoAI voice commands to Home Assistant services.

## Opening the interface

- **Recommended:** the **XiaoAI HA Bridge** entry in the Home Assistant sidebar. It is
  served through HAOS Ingress and uses your Home Assistant login (admins only), so no
  extra password is needed.
- **Direct port:** `http://<haos-host>:47521` still works but now requires the
  direct-access token stored in `/data/config/.access_token`. The token is also
  printed in the add-on log on every start and accepted as
  `Authorization: Bearer <token>`.

## Configuration

- Xiaomi account and speaker (QR code, browser cookie, or account password)
- Home Assistant URL and Long-Lived Access Token
- Voice intent rules
- Device aliases and schedules

This build adds an HA entity selector to intent rules. The list is loaded from the
existing `/api/devices` endpoint and filtered by the selected domain. Hold Ctrl to
select several entities for one action; saved rules apply to the next voice command
without restarting the bridge.

The rules page shows compact summaries by default. Search by spoken phrase, entity ID,
or device name, then click Edit on a rule to inspect or change its details. Unsaved
edits are marked in the UI and are never silently replaced when the device list
refreshes or you switch pages; a failed save keeps your edits and reports the reason.

QR and Cookie logins exchange the Xiaomi account `passToken` for a fresh `micoapi`
service token. A login is only saved after Xiaomi returns a non-empty service token.
If that token expires later, the bridge refreshes it from the saved passToken with
exponential backoff and resumes polling; it never deletes or blanks credentials it
could not verify.

The **清除登录凭据** button on the Xiaomi page deletes `auth.json`, `.mi.token` and
the stored cookie/password so you can start over from a clean state.
