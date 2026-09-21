"""Tests for the reconfigure flow's safety rules.

The cloud client is replaced with a fake so the flow's decisions can be
driven directly. The rules under test are the ones whose failure would cost
a user their entities.
"""
from __future__ import annotations

import asyncio

import pytest
from conftest import load

cf = load("config_flow")


class FakeCloud:
    """Stand-in for CyncCloudClient. Class attributes steer behaviour."""

    devices: list | None = None
    raise_on_fetch = False
    instances: list = []

    def __init__(self) -> None:
        self.restored = None
        self.closed = False
        FakeCloud.instances.append(self)

    def restore_session(self, token, user_id):
        self.restored = (token, user_id)

    async def get_devices(self):
        if FakeCloud.raise_on_fetch:
            raise RuntimeError("cloud unreachable")
        return FakeCloud.devices

    async def request_login_code(self, email):
        return True

    async def authenticate(self, email, password, otp):
        return True

    async def close(self):
        self.closed = True

    @property
    def access_token(self):
        return "fresh-token"

    @property
    def user_id(self):
        return "user-1"

    @classmethod
    def reset(cls, devices=None, raise_on_fetch=False):
        cls.devices = devices
        cls.raise_on_fetch = raise_on_fetch
        cls.instances = []


class FakeEntry:
    def __init__(self, devices, token="stored-token", user_id="user-1") -> None:
        self.data = {
            "email": "me@example.com",
            "session_token": token,
            "user_id": user_id,
            "devices": devices,
            "unrelated_key": "preserve me",
        }
        self.entry_id = "entry-1"


@pytest.fixture(autouse=True)
def _patch_cloud(monkeypatch):
    monkeypatch.setattr(cf, "CyncCloudClient", FakeCloud)
    FakeCloud.reset()


def make_flow(entry):
    flow = cf.CyncBLEConfigFlow()
    flow._get_reconfigure_entry = lambda: entry
    flow.async_show_form = lambda **kw: {"type": "form", **kw}
    flow.async_abort = lambda reason: {"type": "abort", "reason": reason}
    flow.async_update_reload_and_abort = \
        lambda e, data=None: {"type": "updated", "data": data}
    return flow


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Stored-token path
# --------------------------------------------------------------------------

def test_stored_token_reaches_confirm_without_an_otp(make_device):
    FakeCloud.reset(devices=[make_device(),
                             make_device(name="New", device_id=9,
                                         mac="AA:BB:CC:DD:EE:02")])
    result = run(make_flow(FakeEntry([make_device()])).async_step_reconfigure())

    assert result["step_id"] == "reconfigure_confirm"
    assert FakeCloud.instances[0].restored == ("stored-token", "user-1")
    assert FakeCloud.instances[0].closed is True
    assert result["description_placeholders"]["added_count"] == "1"


def test_entry_without_a_stored_token_goes_straight_to_reauth(make_device):
    FakeCloud.reset(devices=[make_device()])
    result = run(
        make_flow(FakeEntry([make_device()], token=None)).async_step_reconfigure()
    )
    assert result["step_id"] == "reconfigure_auth"


# --------------------------------------------------------------------------
# An empty device list is a FAILURE, not "you have no devices"
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("devices", "raises"),
    [([], False), (None, False), (None, True)],
    ids=["empty-list", "none", "exception"],
)
def test_a_bad_fetch_never_reaches_confirm(make_device, devices, raises):
    """Accepting an empty response would offer to wipe the entry."""
    FakeCloud.reset(devices=devices, raise_on_fetch=raises)
    result = run(make_flow(FakeEntry([make_device()])).async_step_reconfigure())
    assert result["step_id"] == "reconfigure_auth"


def test_reauth_path_also_rejects_an_empty_device_list(make_device):
    FakeCloud.reset(devices=[])
    flow = make_flow(FakeEntry([make_device()]))
    flow._email = "me@example.com"
    flow._cloud = FakeCloud()

    result = run(flow.async_step_reconfigure_otp({"otp": "123456"}))
    assert result["step_id"] == "reconfigure_otp"
    assert result["errors"] == {"base": "no_devices_found"}


def test_confirm_aborts_rather_than_applying_an_empty_fetch(make_device):
    flow = make_flow(FakeEntry([make_device()]))
    flow._fetched_devices = []
    assert run(flow.async_step_reconfigure_confirm())["reason"] == "cannot_connect"


# --------------------------------------------------------------------------
# No-op handling
# --------------------------------------------------------------------------

def test_no_differences_aborts_instead_of_showing_an_empty_form(make_device):
    FakeCloud.reset(devices=[make_device()])
    result = run(make_flow(FakeEntry([make_device()])).async_step_reconfigure())
    assert result["reason"] == "no_changes"


def test_a_missing_only_diff_counts_as_no_changes(make_device):
    """Removal is opt-in, so a missing device alone would change nothing."""
    FakeCloud.reset(devices=[make_device()])
    stored = [make_device(),
              make_device(name="Gone", device_id=9, mac="AA:BB:CC:DD:EE:09")]
    result = run(make_flow(FakeEntry(stored)).async_step_reconfigure())
    assert result["reason"] == "no_changes"


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------

def test_apply_merges_and_keeps_missing_devices_by_default(make_device):
    stored = [make_device(name="Old"),
              make_device(name="Gone", device_id=9, mac="AA:BB:CC:DD:EE:09")]
    flow = make_flow(FakeEntry(stored))
    flow._fetched_devices = [make_device(name="Renamed")]

    result = run(flow.async_step_reconfigure_confirm({"remove_missing": False}))
    assert [d["name"] for d in result["data"]["devices"]] == ["Renamed", "Gone"]
    assert result["data"]["unrelated_key"] == "preserve me"


def test_apply_removes_missing_devices_when_opted_in(make_device):
    stored = [make_device(name="Old"),
              make_device(name="Gone", device_id=9, mac="AA:BB:CC:DD:EE:09")]
    flow = make_flow(FakeEntry(stored))
    flow._fetched_devices = [make_device(name="Renamed")]

    result = run(flow.async_step_reconfigure_confirm({"remove_missing": True}))
    assert [d["name"] for d in result["data"]["devices"]] == ["Renamed"]


def test_remove_missing_defaults_to_false_when_the_key_is_absent(make_device):
    flow = make_flow(FakeEntry([make_device(name="Old"),
                                make_device(name="Gone", device_id=9,
                                            mac="AA:BB:CC:DD:EE:09")]))
    flow._fetched_devices = [make_device(name="Renamed")]
    result = run(flow.async_step_reconfigure_confirm({}))
    assert len(result["data"]["devices"]) == 2


def test_the_stored_token_is_not_rewritten_when_reauth_did_not_happen(make_device):
    flow = make_flow(FakeEntry([make_device(name="Old")]))
    flow._fetched_devices = [make_device(name="Renamed")]
    result = run(flow.async_step_reconfigure_confirm({}))
    assert result["data"]["session_token"] == "stored-token"


def test_a_refreshed_token_is_persisted_when_reauth_happened(make_device):
    flow = make_flow(FakeEntry([make_device(name="Old")]))
    flow._fetched_devices = [make_device(name="Renamed")]
    flow._reauth_token, flow._reauth_user_id = "fresh-token", "user-1"
    result = run(flow.async_step_reconfigure_confirm({}))
    assert result["data"]["session_token"] == "fresh-token"


# --------------------------------------------------------------------------
# The email must not be editable
# --------------------------------------------------------------------------

def test_the_reauth_step_offers_no_email_field():
    """An editable email would let a reconfigure repoint the entry at a
    different Cync account, invalidating every entity."""
    fields = {str(k) for k in cf.STEP_RECONFIGURE_AUTH_SCHEMA}
    assert fields == {"password"}


def test_the_email_is_shown_as_a_read_only_placeholder(make_device):
    FakeCloud.reset(devices=[make_device()])
    result = run(make_flow(FakeEntry([make_device()])).async_step_reconfigure_auth())
    assert result["description_placeholders"]["email"] == "me@example.com"
