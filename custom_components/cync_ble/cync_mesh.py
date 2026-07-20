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
    BLE_TIMEOUT,
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
        self._lock = asyncio.Lock()
        self._connecting = False  # guard against concurrent connect attempts

    @property
    def is_connected(self) -> bool:
        return self._connected and self._sk is not None

    async def connect(self, preferred_mac: Optional[str] = None) -> bool:
        """Attempt to connect to a mesh MAC.

        If preferred_mac is given (e.g. from a BLE advertisement callback),
        try that one first and only that one — avoids flooding proxy slots.
        Otherwise iterate through all known MACs.
        """
        if self._connecting or self._connected:
            return self._connected

        self._connecting = True
        try:
            if preferred_mac:
                # Use the device we just saw advertising — single targeted attempt
                mac = self._normalize_mac(preferred_mac)
                _LOGGER.debug("Connecting to mesh '%s' via recently-seen %s", self._mesh_name, mac)
                return await self._connect_to_mac(mac)
            else:
                _LOGGER.debug("Trying to connect to mesh '%s'", self._mesh_name)
                for mac in self._mesh_macs:
                    if await self._connect_to_mac(mac):
                        return True
                return False
        finally:
            self._connecting = False

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
            # establish_connection handles retries and properly routes through
            # ESPHome BLE proxies (requires active: true on the proxy)
            client = await establish_connection(
                BleakClient,
                ble_device,
                mac,
                max_attempts=3,
                disconnected_callback=self._on_disconnected,
            )
            if not client.is_connected:
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
            await client.write_gatt_char(CYNC_PAIRING_CHAR, bytes(packet), response=True)
            await asyncio.sleep(0.3)

            data2 = list(await client.read_gatt_char(CYNC_PAIRING_CHAR))
            if len(data2) < 9:
                _LOGGER.warning(
                    "Pairing response from %s too short (%d bytes) — wrong mesh credentials?",
                    mac, len(data2),
                )
                await client.disconnect()
                return False
            self._sk = _generate_sk(
                self._mesh_name, self._mesh_password,
                random_data, data2[1:9],
            )

            # ---- Enable notifications ----
            await client.start_notify(CYNC_NOTIFY_CHAR, self._on_notification)
            await asyncio.sleep(0.3)
            await client.write_gatt_char(CYNC_NOTIFY_CHAR, bytes([0x01]), response=True)
            await asyncio.sleep(0.3)
            await client.read_gatt_char(CYNC_NOTIFY_CHAR)

            self._connected = True
            _LOGGER.info("Connected to Cync mesh via %s", mac)

            # Ask all devices to report their current state so HA lights
            # immediately reflect reality rather than showing stale defaults.
            try:
                await self.send_packet(0xFFFF, CMD_STATUS_RESPONSE, [0x10])
            except Exception:
                pass  # not fatal — state will populate on next real notification

            return True

        except Exception as err:
            _LOGGER.warning("Failed to connect to %s: %s", mac, err)
            self._sk = None
            self._connected = False
            if self._client:
                try:
                    await self._client.disconnect()
                except Exception:
                    pass
                self._client = None
            return False

    def _on_disconnected(self, _client: Any) -> None:
        """Bleak disconnect callback — called from the BLE stack thread."""
        if self._connected:
            _LOGGER.info("BLE connection to mesh %s dropped", self._mesh_name)
        self._connected = False
        self._sk = None
        self._client = None

    async def disconnect(self) -> None:
        self._connected = False
        self._sk = None
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    async def _on_notification(self, sender: Any, data: bytearray) -> None:
        """Handle status notifications from the mesh."""
        if self._sk is None or self._macdata is None:
            return
        if len(data) < 19:
            return

        pkt = _decrypt_packet(self._sk, self._macdata, list(data))
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
            if self._status_callback:
                try:
                    await self._status_callback(status)
                except Exception as err:
                    _LOGGER.error("Status callback error: %s", err)

    async def send_packet(self, target: int, command: int, data: list[int]) -> bool:
        """Encrypt and send a Telink Mesh packet to a device in the mesh.

        If the connection is stale, reconnects once and retries.
        """
        for attempt in range(2):
            if not self.is_connected:
                if not await self.connect():
                    return False

            async with self._lock:
                if self._sk is None or self._macdata is None:
                    break  # connect failed to produce a session key

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

                enc = _encrypt_packet(self._sk, self._macdata, packet)
                self._packet_count = (self._packet_count + 1) % 65535 or 1

                try:
                    await self._client.write_gatt_char(CYNC_CONTROL_CHAR, bytes(enc))
                    return True
                except Exception as err:
                    _LOGGER.warning("send_packet failed (attempt %d): %s", attempt + 1, err)
                    self._connected = False
                    # loop will retry after reconnecting

        return False

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
