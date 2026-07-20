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

# BLE UUIDs (Cync proprietary)
CYNC_SERVICE_UUID: Final = "00001800-0000-1000-8000-00805f9b34fb"
CYNC_COMMAND_UUID: Final = "0b3e7472-d9d9-11e5-b5d2-0002a5d5c51b"
CYNC_STATUS_UUID: Final = "0b3e7473-d9d9-11e5-b5d2-0002a5d5c51b"
CYNC_NOTIFY_UUID: Final = "0b3e7474-d9d9-11e5-b5d2-0002a5d5c51b"

# Config Keys
CONF_EMAIL: Final = "email"
CONF_PASSWORD: Final = "password"
CONF_SESSION_TOKEN: Final = "session_token"
CONF_DEVICES: Final = "devices"
CONF_DEVICE_ID: Final = "device_id"
CONF_DEVICE_NAME: Final = "device_name"
CONF_DEVICE_TYPE: Final = "device_type"
CONF_MAC_ADDRESS: Final = "mac_address"
CONF_MESH_ID: Final = "mesh_id"

# Device types
DEVICE_TYPE_BULB: Final = "BULB"
DEVICE_TYPE_STRIP: Final = "STRIP"

# Light capabilities
MIN_BRIGHTNESS: Final = 1
MAX_BRIGHTNESS: Final = 254
MIN_COLOR_TEMP: Final = 2000
MAX_COLOR_TEMP: Final = 6500

# BLE Command types
BLE_CMD_POWER: Final = 0x01
BLE_CMD_BRIGHTNESS: Final = 0x02
BLE_CMD_COLOR_TEMP: Final = 0x03
BLE_CMD_RGB: Final = 0x04
BLE_CMD_EFFECT: Final = 0x05

# Timeouts
SCAN_TIMEOUT: Final = 30
BLE_TIMEOUT: Final = 10
CLOUD_TIMEOUT: Final = 10

# Update intervals
POLL_INTERVAL: Final = 60

# Max simultaneous BLE connection attempts across all meshes.
# Telink mesh routing means 1 connection per mesh is sufficient;
# this cap prevents flooding proxy slots during reconnect storms.
MAX_CONCURRENT_CONNECTIONS: Final = 3

# A device is marked unavailable if it hasn't answered a status request in
# this long, even though the shared mesh connection is still up (e.g. it lost
# power). Set well above POLL_INTERVAL so one missed re-poll doesn't flap it.
DEVICE_OFFLINE_TIMEOUT: Final = 180

# After this many consecutive connect failures on a specific mesh MAC, skip
# it for MAC_COOLDOWN_SECONDS so a single bad node can't block reconnection
# to the rest of the mesh.
MAC_FAIL_THRESHOLD: Final = 2
MAC_COOLDOWN_SECONDS: Final = 120

# Attributes
ATTR_SESSION_TOKEN: Final = "session_token"
ATTR_MESH_ID: Final = "mesh_id"
ATTR_DEVICE_ID: Final = "device_id"
