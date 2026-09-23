"""Constants for Cync BLE integration."""
from typing import Final

DOMAIN: Final = "cync_ble"
# binary_sensor/sensor carry the system-status diagnostics (see entity.py)
PLATFORMS: Final = ["light", "switch", "fan", "binary_sensor", "sensor"]

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

# ---------------------------------------------------------------------------
# Mesh-OTA firmware version query (groundwork for OTA; not OTA itself)
#
# The Telink Android SDK manual (AN-17071702-E1 §5 "OTA/MeshOTA", p.8-9)
# documents the read command the app issues to collect firmware versions
# from the whole mesh before starting a MeshOTA:
#
#     opcode 0xC7, dest 0xFFFF, params [0x20, 0x00]  -> every device reports
#                                                       its firmware version
#     opcode 0xC7, dest 0x0000, params [0x20, 0x05]  -> OTA status of the
#                                                       directly-connected device
#
# It is an ordinary mesh command on the 1912 control characteristic, so it
# rides the existing send_packet() path, and replies arrive as notifications
# on 1911 which we already subscribe to.
# ---------------------------------------------------------------------------
CMD_MESH_OTA: Final = 0xC7
# Params[0] — selects the "mesh OTA read" family within opcode 0xC7.
MESH_OTA_SELECTOR_READ: Final = 0x20
# Params[1] — which read. GET_VERSION is what we use; GET_OTA_STATE is kept
# because the manual pairs the two and the eventual OTA flow needs it to
# check the connected device can drive the upgrade before starting one.
MESH_OTA_SUB_GET_VERSION: Final = 0x00
MESH_OTA_SUB_GET_OTA_STATE: Final = 0x05

# INFERRED, NOT VERIFIED. The bundled PDFs document the 0xC7 *request* and
# say only that "firmware version information of device will be available
# via analysis" in the notification event — they never name the reply opcode
# or its payload layout. 0xC8 is LGT_CMD_MESH_OTA_READ_RSP in Telink's own
# light_ll firmware, which makes it the reasonable inference, but it has NOT
# been checked against Cync's vendor fork. Everything downstream of this
# constant is therefore written to be forgiving rather than assertive, and
# always logs the raw parameter bytes so real hardware can confirm or
# correct it — see cync_mesh.decode_firmware_version.
CMD_MESH_OTA_READ_RSP: Final = 0xC8

# How long query_firmware_versions() waits for replies to a 0xFFFF broadcast.
# There is no reply count to await — we do not know up front how many
# devices will answer — so this is simply a collection window.
FIRMWARE_QUERY_WINDOW: Final = 8
# Bounds for the service's optional `window` field.
FIRMWARE_QUERY_WINDOW_MIN: Final = 1
FIRMWARE_QUERY_WINDOW_MAX: Final = 60

# Config Keys
CONF_EMAIL: Final = "email"
CONF_PASSWORD: Final = "password"
CONF_SESSION_TOKEN: Final = "session_token"
CONF_DEVICES: Final = "devices"
# The entry has always stored this key; naming it here so the reconfigure
# flow and the initial flow can't drift apart on its spelling.
CONF_USER_ID: Final = "user_id"
# Reconfigure form field — opt in to dropping devices the cloud no longer
# lists. Defaults off; see device_sync.apply_diff for why.
CONF_REMOVE_MISSING: Final = "remove_missing"

# Repairs issue raised when the mesh reports status from a device that isn't
# in the config entry — i.e. one paired in the Cync app after setup. The
# re-sync only helps if the user finds out they need to run it.
ISSUE_UNKNOWN_DEVICES: Final = "unknown_devices"

# Light capabilities
MIN_COLOR_TEMP: Final = 2000
MAX_COLOR_TEMP: Final = 7000

# Timeouts
# 5s was tight enough that a merely busy ESPHome proxy produced constant
# false timeouts (537 in one 48h window) on writes that would otherwise have
# completed. Still well under bleak's own ~30s default.
BLE_TIMEOUT: Final = 8
CLOUD_TIMEOUT: Final = 10

# Update intervals
POLL_INTERVAL: Final = 60

# How long CyncBLECoordinator.system_status() may reuse its last snapshot.
#
# This exists to collapse a read burst, not to reduce polling. Every
# diagnostic entity reads the snapshot from both its state value and its
# attributes, so one async_update_listeners() dispatch produced 21 full
# recomputations of the same data — all of them inside a single synchronous
# block, so all 21 were guaranteed identical.
#
# A time window rather than explicit invalidation, deliberately: a mesh
# connect or disconnect happens inside CyncMeshClient via bleak's callback
# and is not observable at any coordinator mutation point, so an
# invalidate-on-write scheme would silently serve a stale connection state
# until the next poll. A window can't miss an event it was never told about.
# 50ms is ~1000x longer than a dispatch and ~1000x shorter than POLL_INTERVAL,
# which the rest of the integration already tolerates for connection state.
STATUS_CACHE_TTL: Final = 0.05

# Consecutive failed writes on one GATT session — counted across ALL callers,
# not per send_packet call — before that session is treated as wedged and torn
# down. A timed-out write is only cancelled on our side: the ATT request is
# still outstanding in the proxy and the bulb, and a link allows one at a
# time, so every write queued behind it times out too. That is the exact
# 8.0s-cadence run of timeouts seen before full mesh drops. One timeout on its
# own can still be a merely slow proxy (see BLE_TIMEOUT), so the first is
# tolerated; the second in a row is not.
LINK_STALL_THRESHOLD: Final = 2

# Minimum spacing between consecutive mesh writes. The connected bulb has to
# re-broadcast every command into the mesh (8 repeats by default, per the
# Telink SDK manual §6.5.2) and a burst of back-to-back writes — e.g. Adaptive
# Lighting updating twenty bulbs at once — outruns it. Small enough to be
# invisible on a single command; mostly matters for bursts.
WRITE_MIN_GAP: Final = 0.05

# Liveness probes (see CyncBLEDevice.probe_if_quiet) are held off while any
# light/switch/fan command is queued or has finished within this many
# seconds. Probes are diagnostic and share the single GATT link with user
# commands; competing with a burst of them just adds to the congestion that
# causes the drops probes would then report.
COMMAND_QUIET_PERIOD: Final = 30

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
