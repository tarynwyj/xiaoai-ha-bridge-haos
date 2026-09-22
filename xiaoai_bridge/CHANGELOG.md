# Changelog

## 1.0.9
- Show each intent rule as a compact summary of the phrase, action, and target devices.
- Add rule search and a visible match count; expand only the rule being edited.
- Preserve the multiple-entity selector and make new rules open for editing automatically.

## 1.0.8
- Allow one intent rule to target multiple Home Assistant entities of the same type.
- Limit rule collection to the rules editor, so visiting device or schedule pages does not break saving.
- Read the latest saved rules for each new voice command without restarting Xiaomi login.

## 1.0.7
- Start the bridge with a validated saved `micoapi` service token before attempting account login again.
- Validate device access before saving refreshed credentials, and prevent MiService from deleting the token file on failed login.
- Skip password login when only QR credentials are configured.
- Preserve fresh QR cookies when an older web page saves Xiaomi settings, and prefer the saved token when building polling cookies.
- Let QR-only users run the Xiaomi connection test; display failed QR login as a failure.

## 1.0.6
- Fix the Home Assistant connection test when the saved token is masked in the web UI.
- Reuse a saved token only for its original HA address; require a new token when changing the address.
- Prevent saving a changed HA address with the old masked token, and show the error in the web UI.
- Apply the same behavior to the HA services endpoint.

## 1.0.5
- Allow QR-only accounts to reconnect from saved `passToken` credentials without requiring a stored username and password.
- Reuse the saved QR identity when testing the Xiaomi connection after an app restart or update.

## 1.0.4
- Fix QR login reporting success while saving an empty Xiaomi `serviceToken`.
- Exchange QR `passToken` credentials for a fresh `micoapi` service token before saving.
- Refresh stale Cookie and cached passToken logins through the same validated flow.
- Stop exposing the Xiaomi user ID in QR success messages and logs.

## 1.0.3
- Convert the project into a proper Home Assistant App Store repository.
- Add `repository.yaml` and a dedicated `xiaoai_bridge/config.yaml`.
- Add HA entity drop-downs to intent rules.
- Filter entity choices by Home Assistant domain.
- Preserve an existing entity ID when it is temporarily unavailable.
- Pin the upstream XiaoAI HA Bridge revision for reproducible builds.
- Derive the GHCR tag from the app version automatically.
- Add build-time and post-build smoke checks before publishing the image.
