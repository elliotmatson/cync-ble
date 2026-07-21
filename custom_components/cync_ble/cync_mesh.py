"""Cync BLE mesh protocol — Telink Mesh over GATT.

Protocol verified from cync2mqtt/src/acync/mesh.py.

Key facts:
- Uses AES-ECB for session key negotiation and packet encryption
- Mesh "name" = the mesh's MAC address string (from cloud API)
- Mesh "password" = the mesh's access_key string (from cloud API)
- Three GATT characteristics:
    pairing  : 00010203-0405-0607-0809-0a0b0c0d1914
    control  : 00010203-0405-0607-0809-0a0b0c0d1912
    notify   : 00010203-0405-0607-0809-0a0b0c0d1911
- Vendor ID for Cync: 0x0211
- Commands: 0xD0 (power), 0xD2 (brightness), 0xE2 (color/CT)
- Status notifications: opcode 0xDC
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import namedtuple
from typing import Callable, Optional, Any

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

from bleak_retry_connector import establish_connection
from homeassistant.components.bluetooth import async_ble_device_from_address

from .const import (
    CYNC_NOTIFY_CHAR,
    CYNC_CONTROL_CHAR,
    CYNC_PAIRING_CHAR,
    CYNC_VENDOR,
    CMD_POWER,
    CMD_BRIGHTNESS,
    CMD_COLOR,
    CMD_COLOR_TEMP_SUBCMD,
    CMD_RGB_SUBCMD,
    CMD_STATUS_RESPONSE,
    CMD_STATUS_QUERY,
    CMD_STATUS_QUERY_RESPONSE,
    BLE_TIMEOUT,
    MAC_FAIL_THRESHOLD,
    MAC_COOLDOWN_SECONDS,
    PROBE_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

DeviceStatus = namedtuple(
    "DeviceStatus",
    ["mesh_name", "device_id", "brightness", "is_rgb", "red", "green", "blue", "color_temp"],
)


# ---------------------------------------------------------------------------
# AES helpers — ported directly from acync/mesh.py
# ---------------------------------------------------------------------------

def _aes_encrypt(key: list[int], data: list[int]) -> list[int]:
    k = AES.new(bytes(reversed(key)), AES.MODE_ECB)
    return list(reversed(list(k.encrypt(bytes(reversed(data))))))


def _generate_sk(name: str, password: str, data1: list[int], data2: list[int]) -> list[int]:
    # ljust pads but does NOT truncate — always slice to 16 so AES always gets 16 bytes
    name = name.ljust(16, "\x00")[:16]
    password = password.ljust(16, "\x00")[:16]
    key = [ord(a) ^ ord(b) for a, b in zip(name, password)]
    return _aes_encrypt(key, data1[0:8] + data2[0:8])


def _key_encrypt(name: str, password: str, key: list[int]) -> list[int]:
    name = name.ljust(16, "\x00")[:16]
    password = password.ljust(16, "\x00")[:16]
    data = [ord(a) ^ ord(b) for a, b in zip(name, password)]
    return _aes_encrypt(key, data)


def _encrypt_packet(sk: list[int], address: list[int], packet: list[int]) -> list[int]:
    auth_nonce = [
        address[0], address[1], address[2], address[3], 0x01,
        packet[0], packet[1], packet[2], 15, 0, 0, 0, 0, 0, 0, 0,
    ]
    authenticator = _aes_encrypt(sk, auth_nonce)
    for i in range(15):
        authenticator[i] ^= packet[i + 5]
    mac = _aes_encrypt(sk, authenticator)
    packet[3] = mac[0]
    packet[4] = mac[1]

    iv = [0, address[0], address[1], address[2], address[3], 0x01,
          packet[0], packet[1], packet[2], 0, 0, 0, 0, 0, 0, 0]
    temp_buffer = _aes_encrypt(sk, iv)
    for i in range(15):
        packet[i + 5] ^= temp_buffer[i]
    return packet


def _decrypt_packet(sk: list[int], address: list[int], packet: list[int]) -> list[int]:
    iv = [address[0], address[1], address[2],
          packet[0], packet[1], packet[2], packet[3], packet[4],
          0, 0, 0, 0, 0, 0, 0, 0]
    plaintext = [0] + iv[0:15]
    result = _aes_encrypt(sk, plaintext)
    for i in range(len(packet) - 7):
        packet[i + 7] ^= result[i]
    return packet


# ---------------------------------------------------------------------------
# GATT I/O bounded by BLE_TIMEOUT — bleak/ESPHome's own default (~30s) is far
# longer than send_packet's retry-with-reconnect needs to wait before giving
# up on a hung write and trying again, so a stuck operation used to sit for
# the full 30s before recovery could even start.
# ---------------------------------------------------------------------------

async def _write_gatt(client: Any, char: str, data: bytes, **kwargs: Any) -> None:
    try:
        await asyncio.wait_for(client.write_gatt_char(char, data, **kwargs), timeout=BLE_TIMEOUT)
    except asyncio.TimeoutError:
        # asyncio.wait_for's TimeoutError carries no message on its own —
        # give send_packet's warning log something to actually show.
        raise TimeoutError(f"write to {char} timed out after {BLE_TIMEOUT}s") from None


async def _read_gatt(client: Any, char: str) -> bytearray:
    try:
        return await asyncio.wait_for(client.read_gatt_char(char), timeout=BLE_TIMEOUT)
    except asyncio.TimeoutError:
        raise TimeoutError(f"read from {char} timed out after {BLE_TIMEOUT}s") from None


# ---------------------------------------------------------------------------
# Mesh client
# ---------------------------------------------------------------------------

class CyncMeshClient:
    """BLE client for a single Cync mesh network.

    Uses HA's bluetooth component so commands route through BLE proxies.
    """

    def __init__(
        self,
        hass: Any,
        mesh_name: str,
        mesh_password: str,
        mesh_macs: list[str],
        status_callback: Optional[Callable[[DeviceStatus], Any]] = None,
    ) -> None:
        self._hass = hass
        # Strip colons/dashes so the AES key derivation matches what the bulb expects.
        # The Cync API returns the mesh MAC as "B3AB645F4604" (no colons), and the
        # bulb's pairing key was set using that exact string. If we normalize to
        # "B3:AB:64:5F:46:04" the derived key will be wrong → GATT auth failure.
        self._mesh_name = mesh_name.replace(":", "").replace("-", "").upper()
        self._mesh_password = mesh_password  # access_key string from cloud API
        self._mesh_macs = mesh_macs          # list of bulb MAC addresses
        self._status_callback = status_callback

        self._client = None
        self._sk: Optional[list[int]] = None
        self._macdata: Optional[list[int]] = None
        self._current_mac: Optional[str] = None
        self._packet_count: int = random.randrange(0xFFFF)
        self._connected = False
        self._write_lock = asyncio.Lock()
        # Single-flights connect() so concurrent callers (e.g. two lights on
        # the same mesh commanded at once, or a command racing the periodic
        # poll) share one connection attempt instead of each trying — and
        # instead of extra callers just giving up while the first is busy.
        self._connect_lock = asyncio.Lock()

        # Set by _on_notification when a CMD_STATUS_QUERY_RESPONSE (0xDB)
        # arrives, so query_device_status can tell "no reply" apart from
        # "reply hasn't arrived yet" without the 0xDB payload needing to
        # carry a device_id — only one probe is ever in flight at a time
        # (serialized by _write_lock), so there's nothing else it could be.
        self._probe_reply_event: Optional[asyncio.Event] = None

        # Per-MAC connect-failure tracking, so one persistently bad node
        # (e.g. stuck with a stale GATT cache) can't block reconnection to
        # the rest of the mesh — see _connect_to_mac / _mac_in_cooldown.
        self._mac_fail_counts: dict[str, int] = {}
        self._mac_cooldown_until: dict[str, float] = {}

    @property
    def is_connected(self) -> bool:
        return self._connected and self._sk is not None

    @property
    def is_connecting(self) -> bool:
        return self._connect_lock.locked()

    async def connect(self, preferred_mac: Optional[str] = None) -> bool:
        """Attempt to connect to a mesh MAC.

        If preferred_mac is given (e.g. from a BLE advertisement callback),
        try that one first and only that one — avoids flooding proxy slots.
        Otherwise iterate through all known MACs.

        Single-flighted via _connect_lock: if a connect is already in
        progress, callers wait for it rather than bailing out immediately,
        then reuse its result. Without this, every light command that
        arrives while a reconnect is under way used to fail outright.
        """
        if self._connected:
            return True

        async with self._connect_lock:
            if self._connected:  # another caller finished while we waited
                return True

            if preferred_mac:
                # Use the device we just saw advertising — single targeted attempt
                mac = self._normalize_mac(preferred_mac)
                if self._mac_in_cooldown(mac):
                    _LOGGER.debug(
                        "Skipping %s — in cooldown after repeated connect failures", mac
                    )
                    return False
                _LOGGER.debug("Connecting to mesh '%s' via recently-seen %s", self._mesh_name, mac)
                return await self._connect_to_mac(mac)
            else:
                _LOGGER.debug("Trying to connect to mesh '%s'", self._mesh_name)
                # Try healthy MACs first so one stuck node doesn't eat the whole
                # cycle before we ever reach a bulb that would actually connect.
                # If every MAC is in cooldown, fall back to the full list rather
                # than refusing to reconnect at all.
                candidates = [m for m in self._mesh_macs if not self._mac_in_cooldown(m)]
                if not candidates:
                    candidates = list(self._mesh_macs)
                for mac in candidates:
                    if await self._connect_to_mac(mac):
                        return True
                return False

    def _mac_in_cooldown(self, mac: str) -> bool:
        mac = self._normalize_mac(mac)
        until = self._mac_cooldown_until.get(mac)
        return until is not None and time.monotonic() < until

    def _record_mac_failure(self, mac: str) -> None:
        mac = self._normalize_mac(mac)
        count = self._mac_fail_counts.get(mac, 0) + 1
        self._mac_fail_counts[mac] = count
        if count >= MAC_FAIL_THRESHOLD:
            self._mac_cooldown_until[mac] = time.monotonic() + MAC_COOLDOWN_SECONDS
            _LOGGER.debug(
                "MAC %s failed %d times in a row — cooling down for %ds",
                mac, count, MAC_COOLDOWN_SECONDS,
            )

    def _record_mac_success(self, mac: str) -> None:
        mac = self._normalize_mac(mac)
        self._mac_fail_counts.pop(mac, None)
        self._mac_cooldown_until.pop(mac, None)

    @staticmethod
    def _normalize_mac(mac: str) -> str:
        """Ensure MAC is AA:BB:CC:DD:EE:FF (uppercase, colon-separated)."""
        mac = mac.strip().upper().replace(":", "").replace("-", "")
        if len(mac) == 12:
            return ":".join(mac[i:i+2] for i in range(0, 12, 2))
        return mac

    async def _connect_to_mac(self, mac: str) -> bool:
        """Connect to a specific MAC and perform Telink Mesh pairing."""
        mac = self._normalize_mac(mac)
        try:
            ble_device = async_ble_device_from_address(self._hass, mac, connectable=True)
            if ble_device is None:
                _LOGGER.debug("MAC %s not yet seen by any BLE proxy — skipping", mac)
                return False

            from bleak import BleakClient
            # After a prior failure on this MAC, force fresh GATT service
            # discovery — a stale cached service table (missing a CCCD, etc.)
            # is a common cause of repeated connect failures on one node.
            use_cache = self._mac_fail_counts.get(mac, 0) == 0
            # establish_connection handles retries and properly routes through
            # ESPHome BLE proxies (requires active: true on the proxy)
            client = await establish_connection(
                BleakClient,
                ble_device,
                mac,
                max_attempts=3,
                disconnected_callback=self._on_disconnected,
                use_services_cache=use_cache,
            )
            if not client.is_connected:
                self._record_mac_failure(mac)
                return False

            self._client = client
            self._current_mac = mac

            # macdata: bytes of MAC in reverse order for packet crypto
            parts = mac.split(":")
            self._macdata = [int(p, 16) for p in reversed(parts)]

            # ---- Key exchange (pairing) ----
            random_data = list(get_random_bytes(8))
            data = random_data + [0] * 8
            enc_data = _key_encrypt(self._mesh_name, self._mesh_password, data)

            packet = [0x0C] + data[0:8] + enc_data[0:8]
            _LOGGER.debug("Sending pairing request to %s: %s", mac, bytes(packet).hex())
            await _write_gatt(client, CYNC_PAIRING_CHAR, bytes(packet), response=True)
            await asyncio.sleep(0.3)

            data2 = list(await _read_gatt(client, CYNC_PAIRING_CHAR))
            _LOGGER.debug("Pairing response from %s: %s", mac, bytes(data2).hex())
            if len(data2) < 9:
                _LOGGER.warning(
                    "Pairing response from %s too short (%d bytes) — wrong mesh credentials?",
                    mac, len(data2),
                )
                await client.disconnect()
                self._record_mac_failure(mac)
                return False
            self._sk = _generate_sk(
                self._mesh_name, self._mesh_password,
                random_data, data2[1:9],
            )

            # ---- Enable notifications ----
            await client.start_notify(CYNC_NOTIFY_CHAR, self._on_notification)
            await asyncio.sleep(0.3)
            _LOGGER.debug("Enabling mesh online-status notifications on %s", mac)
            await _write_gatt(client, CYNC_NOTIFY_CHAR, bytes([0x01]), response=True)
            await asyncio.sleep(0.3)
            await _read_gatt(client, CYNC_NOTIFY_CHAR)

            self._connected = True
            self._record_mac_success(mac)
            _LOGGER.info("Connected to Cync mesh via %s", mac)

            # Ask all devices to report their current state so HA lights
            # immediately reflect reality rather than showing stale defaults.
            # Non-blocking — see request_status_nowait.
            self.request_status_nowait()

            return True

        except Exception as err:
            _LOGGER.warning("Failed to connect to %s: %s", mac, err)
            self._record_mac_failure(mac)
            await self._reset_connection_state()
            return False

    def _reset_connection_state_sync(self) -> None:
        """Clear connection state. Callers needing to also close the BLE
        client should use _reset_connection_state() instead — this variant
        exists only for the disconnected_callback, which bleak invokes
        synchronously and can't await.
        """
        self._connected = False
        self._sk = None
        self._client = None

    async def _reset_connection_state(self) -> None:
        """Clear connection state and close the old client, if any.

        Grabs the client reference before clearing it so a concurrent
        send_packet — which reads self._client under _write_lock — never
        sees a half-cleared state (self._connected False but a stale
        self._client still around, or vice versa).
        """
        client = self._client
        self._reset_connection_state_sync()
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    def _on_disconnected(self, _client: Any) -> None:
        """Bleak disconnect callback — invoked synchronously, cannot await."""
        if self._connected:
            _LOGGER.info("BLE connection to mesh %s dropped", self._mesh_name)
        self._reset_connection_state_sync()

    async def disconnect(self) -> None:
        await self._reset_connection_state()

    async def _on_notification(self, sender: Any, data: bytearray) -> None:
        """Handle status notifications from the mesh."""
        _LOGGER.debug("BLE notification received: %s", bytes(data).hex())
        if self._sk is None or self._macdata is None:
            return
        if len(data) < 19:
            return

        pkt = _decrypt_packet(self._sk, self._macdata, list(data))
        _LOGGER.debug("Decrypted notification: %s", bytes(pkt).hex())
        if pkt[7] == CMD_STATUS_QUERY_RESPONSE:
            # Direct reply to query_device_status() — see const.py for why we
            # only treat its arrival as "device answered" for now rather than
            # decoding Params[0:6] into brightness/color.
            _LOGGER.debug(
                "Status query reply (0xDB) received: params=%s",
                bytes(pkt[10:20]).hex(),
            )
            if self._probe_reply_event is not None:
                self._probe_reply_event.set()
            return
        if pkt[7] != CMD_STATUS_RESPONSE:
            return

        # Two device slots packed into response bytes 10–17
        responses = pkt[10:18]
        for i in (0, 4):
            resp = responses[i: i + 4]
            if resp[1] == 0:
                continue
            dev_id = resp[0]
            brightness = resp[2]
            red = green = blue = color_temp = 0
            is_rgb = False

            if brightness >= 128:
                brightness -= 128
                is_rgb = True
                red = int(((resp[3] & 0xE0) >> 5) * 255 / 7)
                green = int(((resp[3] & 0x1C) >> 2) * 255 / 7)
                blue = int((resp[3] & 0x03) * 255 / 3)
            else:
                color_temp = resp[3]

            status = DeviceStatus(
                mesh_name=self._mesh_name,
                device_id=dev_id,
                brightness=brightness,
                is_rgb=is_rgb,
                red=red, green=green, blue=blue,
                color_temp=color_temp,
            )
            _LOGGER.debug("Status notification received: %s", status)
            if self._status_callback:
                try:
                    await self._status_callback(status)
                except Exception as err:
                    _LOGGER.error("Status callback error: %s", err)

    async def send_packet(
        self, target: int, command: int, data: list[int], *, allow_reconnect: bool = True
    ) -> bool:
        """Encrypt and send a Telink Mesh packet to a device in the mesh.

        If the connection is stale, reconnects once and retries — unless
        allow_reconnect is False, in which case a write failure is just
        reported back to the caller. That's used for passive status polling:
        a single missed poll write (proxy hiccup, etc.) shouldn't tear down
        an otherwise-healthy connection or trigger a reconnect. Only a real
        BLE disconnect (via _on_disconnected) or a failed user-issued command
        should do that.
        """
        attempts = 2 if allow_reconnect else 1
        for attempt in range(attempts):
            if not self.is_connected:
                if not allow_reconnect or not await self.connect():
                    return False

            async with self._write_lock:
                # Snapshot under the lock rather than reading self._client /
                # self._sk again below — a disconnect can land between the
                # is_connected check above and here, and we want a single
                # consistent view instead of racing a concurrent reset.
                client, sk, macdata = self._client, self._sk, self._macdata
                if client is None or sk is None or macdata is None:
                    continue  # dropped since the check above — let the loop reconnect

                packet = [0] * 20
                packet[0] = self._packet_count & 0xFF
                packet[1] = (self._packet_count >> 8) & 0xFF
                packet[5] = target & 0xFF
                packet[6] = (target >> 8) & 0xFF
                packet[7] = command
                packet[8] = CYNC_VENDOR & 0xFF
                packet[9] = (CYNC_VENDOR >> 8) & 0xFF
                for i, b in enumerate(data):
                    packet[10 + i] = b

                enc = _encrypt_packet(sk, macdata, packet)
                self._packet_count = (self._packet_count + 1) % 65535 or 1

                _LOGGER.debug(
                    "Sending packet: target=0x%04X command=0x%02X data=%s wire=%s",
                    target, command, data, bytes(enc).hex(),
                )
                try:
                    await _write_gatt(client, CYNC_CONTROL_CHAR, bytes(enc))
                    return True
                except Exception as err:
                    _LOGGER.warning("send_packet failed (attempt %d): %s", attempt + 1, err)
                    if allow_reconnect:
                        await self._reset_connection_state()
                    # loop will retry after reconnecting (if allowed)

        return False

    async def request_status(self) -> bool:
        """Broadcast a status request so every device in the mesh reports in.

        Called once right after connecting. The mesh only pushes status on
        subscribe or on an actual state change (push-on-change, not a
        heartbeat — see the Telink BLE Mesh Lighting APP spec §3.6.2), so
        repeating this later doesn't provoke anything from idle devices; see
        CyncBLEDevice.is_available for how per-device availability is
        derived without relying on that. allow_reconnect=False: see
        send_packet.
        """
        return await self.send_packet(0xFFFF, CMD_STATUS_RESPONSE, [0x10], allow_reconnect=False)

    def request_status_nowait(self) -> None:
        """Fire request_status() in the background without blocking the caller.

        Used right after connecting so a slow proxy write can't delay
        connect() itself.
        """
        self._hass.async_create_task(self._safe_request_status())

    async def _safe_request_status(self) -> None:
        try:
            await self.request_status()
        except Exception as err:
            _LOGGER.debug("Status poll failed: %s", err)

    async def query_device_status(
        self, device_id: int, relay_count: int = 0x10, timeout: float = PROBE_TIMEOUT
    ) -> bool:
        """Ask one specific device to report in (opcode 0xDA) and wait for
        its CMD_STATUS_QUERY_RESPONSE (0xDB) reply, for diagnosing whether a
        quiet device is actually still reachable.

        Unlike request_status's 0xFFFF broadcast, this targets a single
        device_id. Confirmed against real Cync firmware: a reachable device
        answers within ~250ms; an unreachable one produces nothing at all —
        a real liveness signal the push-on-change broadcast can't give us.
        Returns whether a reply arrived in time, not just whether the query
        write succeeded. allow_reconnect=False: a probe is diagnostic —
        getting no reply shouldn't tear down an otherwise-healthy connection.
        """
        self._probe_reply_event = asyncio.Event()
        try:
            if not await self.send_packet(
                device_id, CMD_STATUS_QUERY, [relay_count], allow_reconnect=False
            ):
                return False
            try:
                await asyncio.wait_for(self._probe_reply_event.wait(), timeout=timeout)
                return True
            except asyncio.TimeoutError:
                return False
        finally:
            self._probe_reply_event = None

    # ------------------------------------------------------------------
    # High-level device commands
    # ------------------------------------------------------------------

    async def set_power(self, device_id: int, on: bool) -> bool:
        return await self.send_packet(device_id, CMD_POWER, [int(on)])

    async def set_brightness(self, device_id: int, brightness: int) -> bool:
        """brightness: 0–100"""
        return await self.send_packet(device_id, CMD_BRIGHTNESS, [brightness])

    async def set_color_temp(self, device_id: int, color_temp: int) -> bool:
        """color_temp: 0–100 (caller maps from Kelvin)"""
        return await self.send_packet(device_id, CMD_COLOR, [CMD_COLOR_TEMP_SUBCMD, color_temp])

    async def set_rgb(self, device_id: int, red: int, green: int, blue: int) -> bool:
        return await self.send_packet(device_id, CMD_COLOR, [CMD_RGB_SUBCMD, red, green, blue])
