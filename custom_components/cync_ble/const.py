"""Constants for Cync BLE integration."""
from typing import Final

DOMAIN: Final = "cync_ble"
PLATFORMS: Final = ["light"]

# Cloud API endpoints (GE Lighting / Cync) — verified from cync2mqtt reference
CYNC_CLOUD_URL: Final = "https://api.gelighting.com"
CYNC_OTP_PATH: Final = "/v2/two_factor/email/verifycode"   # POST → sends OTP to email
CYNC_AUTH_PATH: Final = "/v2/user_auth/two_factor"          # POST with OTP → access_token
CYNC_DEVICES_PATH: Final = "/v2/user/{user_id}/subscribe/devices"
CYNC_PROPERTIES_PATH: Final = "/v2/product/{product_id}/device/{device_id}/property"
CYNC_CORP_ID: Final = "1007d2ad150c4000"

# BLE UUIDs — Telink Mesh (verified from cync2mqtt/acync)
CYNC_NOTIFY_CHAR: Final = "00010203-0405-0607-0809-0a0b0c0d1911"
CYNC_CONTROL_CHAR: Final = "00010203-0405-0607-0809-0a0b0c0d1912"
CYNC_PAIRING_CHAR: Final = "00010203-0405-0607-0809-0a0b0c0d1914"

# Telink Mesh vendor ID for Cync
CYNC_VENDOR: Final = 0x0211

# BLE command opcodes
CMD_POWER: Final = 0xD0
CMD_BRIGHTNESS: Final = 0xD2
CMD_COLOR: Final = 0xE2
CMD_COLOR_TEMP_SUBCMD: Final = 0x05
CMD_RGB_SUBCMD: Final = 0x04
CMD_STATUS_RESPONSE: Final = 0xDC
# The Telink spec's documented outbound "ask one device to report in"
# opcode — confirmed working against real Cync firmware: a targeted 0xDA
# query gets back a targeted 0xDB reply within ~250ms when the device is
# reachable, and nothing at all when it isn't. See
# CyncMeshClient.query_device_status.
CMD_STATUS_QUERY: Final = 0xDA
# Direct reply to a CMD_STATUS_QUERY probe — distinct from the CMD_STATUS_RESPONSE
# broadcast. Params[0:6] are PWM channel values, Params[8]=TTC, Params[9]=hops
# per spec, but we haven't verified that channel mapping yet — only used
# today to recognize that a probed device replied at all.
CMD_STATUS_QUERY_RESPONSE: Final = 0xDB

# Config Keys
CONF_EMAIL: Final = "email"
CONF_PASSWORD: Final = "password"
CONF_SESSION_TOKEN: Final = "session_token"
CONF_DEVICES: Final = "devices"

# Light capabilities
MIN_COLOR_TEMP: Final = 2000
MAX_COLOR_TEMP: Final = 7000

# Timeouts
BLE_TIMEOUT: Final = 5
CLOUD_TIMEOUT: Final = 10

# Update intervals
POLL_INTERVAL: Final = 60

# Max simultaneous BLE connection attempts across all meshes.
# Telink mesh routing means 1 connection per mesh is sufficient;
# this cap prevents flooding proxy slots during reconnect storms.
MAX_CONCURRENT_CONNECTIONS: Final = 3

# After this many consecutive connect failures on a specific mesh MAC, skip
# it for MAC_COOLDOWN_SECONDS so a single bad node can't block reconnection
# to the rest of the mesh.
MAC_FAIL_THRESHOLD: Final = 2
MAC_COOLDOWN_SECONDS: Final = 120

# A mesh disconnect this brief or shorter doesn't flip devices unavailable —
# see CyncMeshClient.recently_disconnected. The fast BLE-advertisement-
# triggered reconnect path usually resolves a drop in a few seconds; without
# this, a blip that self-heals before the next slower poll-cycle check still
# gets logged as every device on the mesh going unavailable and immediately
# back, which isn't meaningful and just clutters the log.
RECONNECT_GRACE_PERIOD: Final = 20

# Liveness probing (CyncMeshClient.query_device_status, opcode 0xDA) for
# devices that have gone quiet under the push-on-change protocol — see
# CyncBLEDevice.probe_if_quiet. A device is probed once it's been quiet this
# long, at most once per PROBE_INTERVAL, and marked unavailable after
# PROBE_MISS_THRESHOLD consecutive probes get no reply.
PROBE_QUIET_THRESHOLD: Final = 120
PROBE_INTERVAL: Final = 120
PROBE_MISS_THRESHOLD: Final = 2
PROBE_TIMEOUT: Final = 3.0
