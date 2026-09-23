"""Config flow for Cync BLE integration.

Initial setup is a two-step process:
  Step 1 (user): email + password → Cync sends OTP to email
  Step 2 (otp):  user enters OTP → we get access_token + device list
  Step 3 (devices): user confirms / integration is created

Reconfigure re-syncs the stored device list against the cloud, for bulbs
paired in the Cync app after setup:
  reconfigure         → try the stored token; fall through to re-auth on failure
  reconfigure_auth    → password only (the email is fixed by the entry)
  reconfigure_otp     → OTP, then continue
  reconfigure_confirm → show the diff, apply on submit

Options (CyncBLEOptionsFlow) hold runtime tuning that needs no cloud access —
currently only the mesh write mode.

The diff/merge itself lives in device_sync.py, deliberately free of any HA
imports so it can be reasoned about and tested on its own.
"""
import logging
from typing import Any, Optional

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REMOVE_MISSING,
    CONF_SESSION_TOKEN,
    CONF_USER_ID,
    CONF_WRITE_WITHOUT_RESPONSE,
    DOMAIN,
)
from .cync_cloud import CyncCloudClient
from .device_sync import apply_diff, diff_devices, has_changes, summarize

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

# Password only, no email field. The email is fixed by the entry being
# reconfigured: making it editable here would let a reconfigure silently
# repoint an existing entry at a different Cync account, replacing every
# device under the same entry and invalidating every entity built from the
# old account's MACs. Changing accounts is an add-a-new-entry operation.
STEP_RECONFIGURE_AUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): str,
    }
)

STEP_RECONFIGURE_CONFIRM_SCHEMA = vol.Schema(
    {
        # Defaults to False, and is an explicit opt-in. See device_sync.apply_diff.
        vol.Optional(CONF_REMOVE_MISSING, default=False): bool,
    }
)


class CyncBLEConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Cync BLE."""

    VERSION = 1

    def __init__(self) -> None:
        self._email: Optional[str] = None
        self._password: Optional[str] = None
        self._cloud: Optional[CyncCloudClient] = None

        # Reconfigure state, carried between the reconfigure_* steps.
        self._fetched_devices: Optional[list[dict[str, Any]]] = None
        # Set only when reconfigure had to re-authenticate, so the confirm
        # step knows whether it has a fresher token worth persisting.
        self._reauth_token: Optional[str] = None
        self._reauth_user_id: Optional[str] = None

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
                            CONF_USER_ID: self._cloud.user_id,
                            CONF_DEVICES: devices,
                        },
                    )

        return self.async_show_form(
            step_id="otp",
            data_schema=STEP_OTP_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._email},
        )

    # ------------------------------------------------------------------
    # Reconfigure — re-sync the stored device list against the cloud
    #
    # The cloud device list is captured into the config entry at setup and
    # never refreshed, so a bulb paired in the Cync app afterwards is
    # invisible to the integration: it shows up only as "unrecognized
    # device" warnings when it reports status, and before this the only
    # remedy was deleting and re-adding the whole integration, losing every
    # entity id and everything referencing them.
    # ------------------------------------------------------------------
    async def async_step_reconfigure(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        """Try the stored token first, so the common case needs no OTP."""
        entry = self._get_reconfigure_entry()
        self._email = entry.data.get(CONF_EMAIL)

        token = entry.data.get(CONF_SESSION_TOKEN)
        user_id = entry.data.get(CONF_USER_ID)

        if token and user_id:
            cloud = CyncCloudClient()
            cloud.restore_session(token, user_id)
            try:
                devices = await cloud.get_devices()
            except Exception as err:
                _LOGGER.debug("Stored-token device fetch failed: %s", err)
                devices = None
            finally:
                await cloud.close()

            # An EMPTY list is treated as a failure, not as "you have no
            # devices". A partial or degraded API response is far more
            # likely than a user genuinely owning zero Cync devices while
            # running this integration, and accepting it here would offer to
            # wipe the entry — see also async_step_reconfigure_confirm.
            if devices:
                self._fetched_devices = devices
                return await self.async_step_reconfigure_confirm()

            # Fall through to re-auth. We cannot tell an expired token apart
            # from a cloud outage: the API returns no error specific enough
            # to distinguish them, so rather than guess we offer re-auth,
            # which is the recovery for one and harmless for the other.
            _LOGGER.debug("Stored token did not yield devices — offering re-auth")

        return await self.async_step_reconfigure_auth()

    async def async_step_reconfigure_auth(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        """Re-authenticate. Password only — the email is fixed by the entry."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        self._email = entry.data.get(CONF_EMAIL)

        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            self._cloud = CyncCloudClient()
            try:
                ok = await self._cloud.request_login_code(self._email)
            except Exception as err:
                _LOGGER.exception("Unexpected error requesting OTP: %s", err)
                ok = False

            if ok:
                return await self.async_step_reconfigure_otp()
            errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="reconfigure_auth",
            data_schema=STEP_RECONFIGURE_AUTH_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._email or ""},
        )

    async def async_step_reconfigure_otp(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        """Verify the OTP, then fetch the device list and continue."""
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
                try:
                    devices = await self._cloud.get_devices()
                except Exception as err:
                    _LOGGER.exception("Error fetching devices: %s", err)
                    devices = None

                # Keep the fresh token so the entry doesn't have to go
                # through this again next time.
                self._reauth_token = self._cloud.access_token
                self._reauth_user_id = self._cloud.user_id
                await self._cloud.close()

                # Empty is a failure, not an answer — see async_step_reconfigure.
                if not devices:
                    errors["base"] = "no_devices_found"
                else:
                    self._fetched_devices = devices
                    return await self.async_step_reconfigure_confirm()

        return self.async_show_form(
            step_id="reconfigure_otp",
            data_schema=STEP_OTP_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._email or ""},
        )

    async def async_step_reconfigure_confirm(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        """Show what would change, and apply it on submit."""
        entry = self._get_reconfigure_entry()
        stored: list[dict[str, Any]] = list(entry.data.get(CONF_DEVICES, []))
        fetched = self._fetched_devices or []

        # Defensive: the earlier steps already treat an empty fetch as a
        # failure, but never let this step run against one — with
        # remove_missing on it would otherwise wipe every device.
        if not fetched:
            return self.async_abort(reason="cannot_connect")

        diff = diff_devices(stored, fetched)

        if user_input is not None:
            remove_missing: bool = user_input.get(CONF_REMOVE_MISSING, False)
            new_devices = apply_diff(stored, fetched, remove_missing=remove_missing)

            data = {**entry.data, CONF_DEVICES: new_devices}
            # Only overwrite the token if we actually obtained a fresh one;
            # a successful stored-token path leaves the existing one alone.
            if self._reauth_token and self._reauth_user_id:
                data[CONF_SESSION_TOKEN] = self._reauth_token
                data[CONF_USER_ID] = self._reauth_user_id

            _LOGGER.info(
                "Device re-sync: %d added, %d changed, %d removed (remove_missing=%s), "
                "%d devices stored",
                len(diff["added"]), len(diff["changed"]),
                len(diff["removed"]) if remove_missing else 0,
                remove_missing, len(new_devices),
            )
            # Reloads the entry, which rebuilds the coordinator against the
            # new device list — and, via CyncBLECoordinator.async_shutdown,
            # dismisses any outstanding unknown-devices repair issue.
            return self.async_update_reload_and_abort(entry, data=data)

        # Nothing differs. Abort with a clear reason rather than showing a
        # form whose only honest content is "no changes" — and note this
        # deliberately ignores `removed` (see device_sync.has_changes),
        # because with remove_missing off, missing devices change nothing.
        if not has_changes(diff):
            return self.async_abort(reason="no_changes")

        return self.async_show_form(
            step_id="reconfigure_confirm",
            data_schema=STEP_RECONFIGURE_CONFIRM_SCHEMA,
            description_placeholders=summarize(diff),
        )

    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "CyncBLEOptionsFlow":
        return CyncBLEOptionsFlow(config_entry)


class CyncBLEOptionsFlow(config_entries.OptionsFlow):
    """Runtime tuning that doesn't need the cloud: currently the write mode.

    Saving reloads the entry (see _async_options_updated in __init__.py),
    because the write mode is fixed when the mesh clients are built.
    """

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        # Stored under our own name rather than as `config_entry`, which
        # newer Home Assistant versions provide themselves and warn about
        # being assigned.
        self._entry = entry

    async def async_step_init(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self._entry.options.get(CONF_WRITE_WITHOUT_RESPONSE, False)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Optional(CONF_WRITE_WITHOUT_RESPONSE, default=current): bool}
            ),
        )
