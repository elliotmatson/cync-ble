# Cync BLE — Home Assistant Integration

A native Bluetooth Low Energy integration for **Cync (C by GE) smart bulbs** using Home Assistant's built-in Bluetooth stack. No hub, no cloud dependency after initial setup, and fully compatible with ESPHome BLE proxies to extend range across your home.

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

---

## Features

- On/off, brightness, color temperature, and RGB control
- Push-based state updates via BLE mesh notifications (no polling)
- Works with ESPHome BLE proxies (active scanning required)
- Supports multiple bulbs per mesh and multiple meshes
- Initial cloud authentication only — all subsequent control is local

## How it works

Cync bulbs communicate over a **Telink BLE mesh** — a proprietary mesh protocol where one BLE connection routes commands to all bulbs on the same mesh. This integration connects to whichever mesh node is nearest (as seen by your BLE proxies), pairs using AES-ECB session key negotiation derived from your mesh credentials, and then sends encrypted Telink Mesh packets directly over GATT.

Cloud credentials are used once during setup to retrieve mesh names, access keys, and device IDs. After that, all control is local over Bluetooth.

## Requirements

- Home Assistant 2024.1 or newer
- The **Bluetooth** integration enabled in HA (built-in)
- At least one Bluetooth adapter or **ESPHome BLE proxy** within range of your bulbs
- A Cync account with your bulbs already set up in the Cync app

### ESPHome BLE proxy setup

If your bulbs are out of range of the HA host, ESPHome BLE proxies extend coverage. Each proxy must have **active scanning** enabled:

```yaml
bluetooth_proxy:
  active: true
```

This is the default in recent ESPHome versions. You can run as many proxies as you need; HA will automatically use whichever one can see a given bulb.

## Installation

### Via HACS (recommended)

1. In HACS, go to **Integrations → ⋮ → Custom repositories**
2. Add `https://github.com/elliotmatson/cync-ble` with category **Integration**
3. Install **Cync BLE** from the HACS integration list
4. Restart Home Assistant

### Manual

1. Copy the `custom_components/cync_ble/` folder into your HA `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Cync BLE**
3. Enter your Cync account email and password
4. Check your email for a one-time code and enter it
5. The integration will discover your bulbs and add them as light entities

Authentication sends a one-time passcode to your email — the integration does not store your password.

## Supported devices

Any Cync (C by GE) bulb that uses the Telink BLE mesh protocol should work. This includes most Cync A19, BR30, and smart switch products sold after ~2020. Devices that use Wi-Fi only (no Bluetooth) are not supported.

> **Note:** Device capability detection (RGB vs. color temperature vs. brightness only) comes from the Cync cloud API. If a bulb is miscategorized, file an issue with the device model.

## Troubleshooting

**Lights show as Unavailable**
BLE proxy hasn't seen the bulb yet. Make sure the bulb is powered on and at least one proxy has `active: true`. The integration connects automatically when a proxy sees a bulb advertising.

**"Insufficient authorization" in logs**
Your mesh credentials may be stale. Remove and re-add the integration to re-authenticate.

**Lights connect but commands are slow or drop**
Each mesh only needs one active BLE connection — Telink mesh routing delivers commands to all bulbs through it. If you have multiple meshes, you may need additional proxies.

## Removal

1. Go to **Settings → Devices & Services → Cync BLE**
2. Click the three-dot menu → **Delete**
3. If installed via HACS, go to **HACS → Integrations → Cync BLE → Remove**
4. Restart Home Assistant

Removing the integration does not affect your bulbs or their configuration in the Cync app.

## Known limitations

- State is pushed via BLE notifications; if HA restarts while all bulbs are off, the initial state shown will be the default (on, 100% brightness) until the first notification arrives.
- The integration does not support Cync devices that communicate exclusively over Wi-Fi or Matter.
- Scenes and schedules configured in the Cync app are not surfaced in HA.

## Credits

Protocol implementation based on [cync2mqtt](https://github.com/juanboro/cync2mqtt) by juanboro, which reverse-engineered the Telink Mesh BLE protocol and Cync cloud API. This integration ports that work into the native HA Bluetooth stack.

## AI Disclosure

This integration was developed with substantial assistance from **Claude** (Anthropic), an AI assistant. Claude wrote the majority of the code, debugged protocol issues, and iteratively fixed errors based on Home Assistant logs. The human author provided requirements, tested against real hardware, shared logs, and made decisions about the design — but did not write most of the code by hand.

If you find bugs or want to contribute, please open an issue or pull request. AI-generated code can have subtle errors that only surface in edge cases, and real-world testing feedback is especially valuable.

## License

MIT
