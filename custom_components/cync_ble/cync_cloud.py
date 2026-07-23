"""Cync Cloud API client — endpoints verified from cync2mqtt/acync reference.

Auth flow:
  1. POST /v2/two_factor/email/verifycode  →  Cync emails a one-time code
  2. POST /v2/user_auth/two_factor  (with OTP)  →  access_token + user_id
  3. GET  /v2/user/{user_id}/subscribe/devices  (Access-Token header)  →  mesh list
  4. GET  /v2/product/{pid}/device/{did}/property  →  bulb details per mesh
"""
import logging
import random
import string
from typing import Optional, Any

import aiohttp

from .const import (
    CYNC_CLOUD_URL,
    CYNC_OTP_PATH,
    CYNC_AUTH_PATH,
    CYNC_DEVICES_PATH,
    CYNC_PROPERTIES_PATH,
    CYNC_CORP_ID,
    CLOUD_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


def _random_resource(length: int = 16) -> str:
    """Generate a random resource string, matching cync2mqtt's randomLoginResource()."""
    return "".join(random.choices(string.ascii_lowercase, k=length))


class CyncCloudClient:
    """Cync cloud API client."""

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._access_token: Optional[str] = None
        self._user_id: Optional[str] = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # ------------------------------------------------------------------
    # Step 1: request OTP — POST /v2/two_factor/email/verifycode
    # ------------------------------------------------------------------
    async def request_login_code(self, email: str) -> bool:
        """Ask Cync to email a one-time login code to the user."""
        payload = {
            "corp_id": CYNC_CORP_ID,
            "email": email,
            "local_lang": "en-us",
        }
        try:
            async with self._get_session().post(
                CYNC_CLOUD_URL + CYNC_OTP_PATH,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=CLOUD_TIMEOUT),
            ) as resp:
                body = await resp.text()
                _LOGGER.debug("request_login_code status=%d body=%s", resp.status, body)
                if resp.status != 200:
                    _LOGGER.error("OTP request failed: HTTP %d — %s", resp.status, body)
                    return False
                return True
        except aiohttp.ClientError as err:
            _LOGGER.error("Network error requesting OTP: %s", err)
            return False

    # ------------------------------------------------------------------
    # Step 2: verify OTP — POST /v2/user_auth/two_factor
    # ------------------------------------------------------------------
    async def authenticate(self, email: str, password: str, otp: str) -> bool:
        """Submit the emailed OTP; on success stores access_token and user_id."""
        payload = {
            "corp_id": CYNC_CORP_ID,
            "email": email,
            "password": password,
            "two_factor": otp,
            "resource": _random_resource(),
        }
        try:
            async with self._get_session().post(
                CYNC_CLOUD_URL + CYNC_AUTH_PATH,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=CLOUD_TIMEOUT),
            ) as resp:
                body = await resp.text()
                _LOGGER.debug("authenticate status=%d body=%s", resp.status, body)
                if resp.status != 200:
                    _LOGGER.error("Auth failed: HTTP %d — %s", resp.status, body)
                    return False

                data = await resp.json(content_type=None)
                self._access_token = data.get("access_token")
                self._user_id = str(data.get("user_id", ""))

                if not self._access_token or not self._user_id:
                    _LOGGER.error("Missing access_token/user_id in response: %s", data)
                    return False

                return True
        except aiohttp.ClientError as err:
            _LOGGER.error("Network error during auth: %s", err)
            return False

    # ------------------------------------------------------------------
    # Step 3: device list — GET /v2/user/{user_id}/subscribe/devices
    # Step 4: per-mesh properties — GET /v2/product/{pid}/device/{did}/property
    # ------------------------------------------------------------------
    async def get_devices(self) -> Optional[list[dict[str, Any]]]:
        """Return a flat list of bulbs across all mesh networks.

        Each entry contains: name, device_id, mac, mesh_name (= mesh MAC),
        access_key, supports_rgb, supports_temperature, device_type.
        """
        if not self._access_token or not self._user_id:
            _LOGGER.error("get_devices called before successful authentication")
            return None

        # NOTE: The API uses 'Access-Token', NOT 'Authorization: Bearer'
        headers = {"Access-Token": self._access_token}
        url = CYNC_CLOUD_URL + CYNC_DEVICES_PATH.format(user_id=self._user_id)

        try:
            async with self._get_session().get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=CLOUD_TIMEOUT),
            ) as resp:
                body = await resp.text()
                _LOGGER.debug("get_devices status=%d", resp.status)
                if resp.status != 200:
                    _LOGGER.error("get_devices failed: HTTP %d — %s", resp.status, body)
                    return None
                meshes = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            _LOGGER.error("Network error fetching devices: %s", err)
            return None

        devices: list[dict[str, Any]] = []

        for mesh in meshes if isinstance(meshes, list) else []:
            mesh_name = mesh.get("name", "")
            mesh_mac = _normalize_mac(mesh.get("mac", ""))
            access_key = str(mesh.get("access_key", ""))
            product_id = mesh.get("product_id")
            mesh_id = mesh.get("id")

            if not mesh_name or not mesh_mac:
                continue

            # Fetch per-device properties for this mesh
            properties = await self._get_properties(headers, product_id, mesh_id)
            bulbs = (properties or {}).get("bulbsArray", [])

            for bulb in bulbs:
                device_id_raw = bulb.get("deviceID", "")
                # The local mesh device ID is the last 3 digits of deviceID
                try:
                    local_id = int(str(device_id_raw)[-3:])
                except (ValueError, TypeError):
                    local_id = 0

                dtype = bulb.get("deviceType", 0)
                devices.append({
                    "name": bulb.get("displayName", f"Cync {local_id}"),
                    "device_id": local_id,
                    "mac": _normalize_mac(bulb.get("mac", "")),
                    "mesh_name": mesh_mac,   # used as BLE mesh "name" for key exchange
                    "access_key": access_key,  # used as BLE mesh "password"
                    "mesh_display_name": mesh_name,
                    "device_type": dtype,
                    "supports_rgb": dtype in Capabilities["RGB"],
                    "supports_temperature": dtype in Capabilities["COLORTEMP"],
                    "is_plug": dtype in Capabilities["PLUG"],
                    "is_fan": dtype in Capabilities["FAN"],
                })

        _LOGGER.debug("Found %d bulbs across %d meshes", len(devices), len(meshes) if isinstance(meshes, list) else 0)
        return devices

    async def _get_properties(
        self, headers: dict, product_id: Any, device_id: Any
    ) -> Optional[dict[str, Any]]:
        """GET /v2/product/{product_id}/device/{device_id}/property"""
        if product_id is None or device_id is None:
            return None
        url = CYNC_CLOUD_URL + CYNC_PROPERTIES_PATH.format(
            product_id=product_id, device_id=device_id
        )
        try:
            async with self._get_session().get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=CLOUD_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
        except aiohttp.ClientError:
            return None

    # ------------------------------------------------------------------
    # Properties / cleanup
    # ------------------------------------------------------------------
    @property
    def access_token(self) -> Optional[str]:
        return self._access_token

    @property
    def user_id(self) -> Optional[str]:
        return self._user_id

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None


def _normalize_mac(mac: str) -> str:
    """Ensure MAC is in AA:BB:CC:DD:EE:FF format (uppercase, with colons)."""
    mac = mac.strip().upper().replace(":", "").replace("-", "")
    if len(mac) == 12:
        return ":".join(mac[i:i+2] for i in range(0, 12, 2))
    return mac  # return as-is if unexpected format


# Device type → capability lookup, verbatim from nikshriv/cync_lights'
# cync_hub.py (https://github.com/nikshriv/cync_lights) — only RGB and
# COLORTEMP are consumed today, but kept as one unmodified table so the
# rest (PLUG, FAN, MOTION, AMBIENT_LIGHT, MULTIELEMENT, ...) is ready to
# wire up without re-deriving the data again.
Capabilities = {
    "ONOFF":[1,5,6,7,8,9,10,11,13,14,15,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,47,48,49,51,52,53,54,55,56,57,58,59,61,62,63,64,65,66,67,68,80,81,82,83,85,128,129,130,131,132,133,134,135,136,137,138,139,140,141,142,143,144,145,146,147,148,149,150,151,152,153,154,155,156,158,159,160,161,162,163,164,165,166,169,170,171,172],
    "BRIGHTNESS":[1,5,6,7,8,9,10,11,13,14,15,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36,37,47,48,49,55,56,80,81,82,83,85,128,129,130,131,132,133,134,135,136,137,138,139,140,141,142,143,144,145,146,147,148,149,150,151,152,153,154,155,156,158,159,160,161,162,163,164,165,166,169,170,171],
    "COLORTEMP":[5,6,7,8,10,11,14,15,19,20,21,22,23,25,26,28,29,30,31,32,33,34,35,47,80,82,83,85,129,130,131,132,133,135,136,137,138,139,140,141,142,143,144,145,146,147,153,154,155,156,158,159,160,161,162,163,164,165,166,169,170,171],
    "RGB":[6,7,8,21,22,23,30,31,32,33,34,35,47,131,132,133,137,138,139,140,141,142,143,146,147,153,154,155,156,158,159,160,161,162,163,164,165,166,169,170,171],
    "MOTION":[37,49,54],
    "AMBIENT_LIGHT":[37,49,54],
    "WIFICONTROL":[36,37,38,39,40,47,48,49,51,52,53,54,55,56,57,58,59,61,62,63,64,65,66,67,68,80,81,128,129,130,131,132,133,134,135,136,137,138,139,140,141,142,143,144,145,146,147,148,149,150,151,152,153,154,155,156,158,159,160,161,162,163,164,165,166,169,170,171,172],
    "PLUG":[64,65,66,67,68,172],
    "FAN":[81],
    "MULTIELEMENT":{'67':2}
}
