"""Config flow for Cync BLE integration.

Auth is a two-step process:
  Step 1 (user): email + password → Cync sends OTP to email
  Step 2 (otp):  user enters OTP → we get access_token + device list
  Step 3 (devices): user confirms / integration is created
"""
import logging
from typing import Any, Optional

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN, CONF_EMAIL, CONF_PASSWORD, CONF_SESSION_TOKEN, CONF_DEVICES
from .cync_cloud import CyncCloudClient

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

STEP_OTP_SCHEMA = vol.Schema(
    {
        vol.Required("otp"): str,
    }
)


class CyncBLEConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Cync BLE."""

    VERSION = 1

    def __init__(self) -> None:
        self._email: Optional[str] = None
        self._password: Optional[str] = None
        self._cloud: Optional[CyncCloudClient] = None

    # ------------------------------------------------------------------
    # Step 1 — collect credentials, request OTP
    # ------------------------------------------------------------------
    async def async_step_user(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input[CONF_EMAIL]
            self._password = user_input[CONF_PASSWORD]

            # Prevent the same account from being set up twice
            await self.async_set_unique_id(self._email.lower())
            self._abort_if_unique_id_configured()

            self._cloud = CyncCloudClient()

            try:
                ok = await self._cloud.request_login_code(self._email)
            except Exception as err:
                _LOGGER.exception("Unexpected error requesting OTP: %s", err)
                ok = False

            if ok:
                # OTP was sent — move to next step
                return await self.async_step_otp()
            else:
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 2 — collect OTP, authenticate, fetch devices
    # ------------------------------------------------------------------
    async def async_step_otp(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            otp = user_input["otp"].strip()
            try:
                ok = await self._cloud.authenticate(self._email, self._password, otp)
            except Exception as err:
                _LOGGER.exception("Unexpected error during OTP verification: %s", err)
                ok = False

            if not ok:
                errors["base"] = "invalid_auth"
            else:
                # Fetch device list
                try:
                    devices = await self._cloud.get_devices()
                except Exception as err:
                    _LOGGER.exception("Error fetching devices: %s", err)
                    devices = None

                await self._cloud.close()

                if devices is None:
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=f"Cync ({self._email})",
                        data={
                            CONF_EMAIL: self._email,
                            CONF_SESSION_TOKEN: self._cloud.access_token,
                            "user_id": self._cloud.user_id,
                            CONF_DEVICES: devices,
                        },
                    )

        return self.async_show_form(
            step_id="otp",
            data_schema=STEP_OTP_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._email},
        )
