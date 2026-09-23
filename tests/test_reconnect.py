"""Reconnect sweeps, per-bulb cooldowns and the write-mode option.

The reconnect behaviour these pin down, from a real morning's log: after
every drop the old sweep walked all 44 bulbs in config order with three
20-second attempts each. Every attempt pauses scanning on the ESPHome proxy
making it, so the sweep kept the proxies deaf for minutes, the few bulbs
that always failed (7-13 times each) were retried on every sweep, and
advertisements that arrived in between each kicked off another sweep.

Sweeps now use only bulbs a proxy has heard recently, strongest first, a few
at a time, and back off after failing. The ranking reads HA's
async_last_service_info, which these tests replace with a table.
"""
from __future__ import annotations

import asyncio
import time
import types

import pytest
from conftest import load

cm = load("cync_mesh")
const = load("const")
config_flow = load("config_flow")

MACS = [f"AA:BB:CC:DD:EE:{i:02X}" for i in range(1, 9)]


def make_client(**kwargs) -> "cm.CyncMeshClient":
    return cm.CyncMeshClient(
        hass=None, mesh_name="112233445566", mesh_password="pw",
        mesh_macs=list(MACS), **kwargs,
    )


@pytest.fixture
def heard(monkeypatch):
    """Map MAC -> (rssi, seconds since last advertisement)."""
    table: dict[str, tuple[int, float]] = {}

    def last_service_info(hass, mac, connectable=True):
        if mac not in table:
            return None
        rssi, age = table[mac]
        return types.SimpleNamespace(rssi=rssi, time=time.monotonic() - age)

    monkeypatch.setattr(cm, "async_last_service_info", last_service_info)
    return table


def record_attempts(client, succeed_on=()):
    attempts: list[str] = []

    async def fake_connect_to_mac(mac):
        attempts.append(mac)
        if mac in succeed_on:
            client._connected = True
            client._sk = [0] * 16
            return True
        client._record_mac_failure(mac)
        return False

    client._connect_to_mac = fake_connect_to_mac
    return attempts


# --------------------------------------------------------------------------
# Candidate ranking
# --------------------------------------------------------------------------

def test_candidates_are_recently_heard_bulbs_strongest_first(heard):
    heard[MACS[0]] = (-80, 5)
    heard[MACS[1]] = (-50, 5)
    heard[MACS[2]] = (-65, 5)
    client = make_client()

    assert client._rank_candidates() == [MACS[1], MACS[2], MACS[0]]


def test_bulbs_not_heard_recently_are_not_tried(heard):
    heard[MACS[0]] = (-40, const.RECONNECT_ADVERT_MAX_AGE + 10)  # strong but stale
    heard[MACS[1]] = (-90, 5)
    client = make_client()

    assert client._rank_candidates() == [MACS[1]]


def test_sweep_is_capped(heard):
    for i, mac in enumerate(MACS):
        heard[mac] = (-50 - i, 5)
    client = make_client()

    assert client._rank_candidates() == MACS[: const.RECONNECT_SWEEP_MAX]


def test_bulbs_in_cooldown_are_skipped(heard):
    heard[MACS[0]] = (-40, 5)
    heard[MACS[1]] = (-70, 5)
    client = make_client()
    for _ in range(const.MAC_FAIL_THRESHOLD):
        client._record_mac_failure(MACS[0])

    assert client._rank_candidates() == [MACS[1]]


def test_when_every_heard_bulb_is_cooling_down_the_least_failed_is_retried(heard):
    heard[MACS[0]] = (-40, 5)
    heard[MACS[1]] = (-70, 5)
    client = make_client()
    for _ in range(5):
        client._record_mac_failure(MACS[0])
    for _ in range(const.MAC_FAIL_THRESHOLD):
        client._record_mac_failure(MACS[1])

    assert client._rank_candidates() == [MACS[1]]


def test_nothing_heard_means_no_candidates(heard):
    assert make_client()._rank_candidates() == []


# --------------------------------------------------------------------------
# Cooldown escalation
# --------------------------------------------------------------------------

def _cooldown(client, mac):
    return client._mac_cooldown_until[mac] - time.monotonic()


def test_cooldown_doubles_with_each_further_failure_up_to_the_cap():
    client = make_client()
    mac = MACS[0]
    seen = []
    for _ in range(const.MAC_FAIL_THRESHOLD + 5):
        client._record_mac_failure(mac)
        if mac in client._mac_cooldown_until:
            seen.append(round(_cooldown(client, mac)))

    base = const.MAC_COOLDOWN_SECONDS
    assert seen[:3] == [base, base * 2, base * 4]
    assert max(seen) == const.MAC_COOLDOWN_MAX_SECONDS


def test_success_clears_the_cooldown():
    client = make_client()
    for _ in range(4):
        client._record_mac_failure(MACS[0])
    client._record_mac_success(MACS[0])

    assert not client._mac_in_cooldown(MACS[0])
    assert MACS[0] not in client._mac_fail_counts


# --------------------------------------------------------------------------
# Sweep behaviour and backoff
# --------------------------------------------------------------------------

def test_sweep_tries_only_ranked_candidates(heard):
    heard[MACS[3]] = (-60, 5)
    heard[MACS[5]] = (-50, 5)
    client = make_client()
    attempts = record_attempts(client)

    assert asyncio.run(client.connect()) is False
    assert attempts == [MACS[5], MACS[3]]


def test_failed_sweep_backs_off_and_callers_fail_fast(heard):
    heard[MACS[0]] = (-60, 5)
    client = make_client()
    attempts = record_attempts(client)

    asyncio.run(client.connect())
    assert not client.can_attempt_connect
    # During the backoff a caller (e.g. a light command) gets False at once
    # instead of starting another sweep that pauses the proxies again.
    assert asyncio.run(client.connect()) is False
    assert attempts == [MACS[0]]


def test_backoff_doubles_to_a_cap_and_resets_on_success(heard):
    heard[MACS[0]] = (-60, 5)
    client = make_client()
    record_attempts(client)

    backoffs = []
    for _ in range(6):
        client._next_sweep_at = 0.0  # let the next sweep run immediately
        asyncio.run(client.connect())
        backoffs.append(client._sweep_backoff)

    assert backoffs[0] == const.RECONNECT_BACKOFF_MIN
    assert backoffs[1] == const.RECONNECT_BACKOFF_MIN * 2
    assert backoffs[-1] == const.RECONNECT_BACKOFF_MAX

    heard[MACS[1]] = (-40, 5)
    record_attempts(client, succeed_on={MACS[1]})
    client._next_sweep_at = 0.0
    assert asyncio.run(client.connect()) is True
    assert client._sweep_backoff == 0.0
    assert client._next_sweep_at == 0.0


def test_empty_sweep_still_backs_off(heard):
    """Nothing heard (e.g. right after HA starts, before the proxies report
    in) must not turn into a tight loop of empty sweeps."""
    client = make_client()
    attempts = record_attempts(client)

    assert asyncio.run(client.connect()) is False
    assert attempts == []
    assert client._next_sweep_at > time.monotonic()


def test_debug_state_reports_backoff_and_write_mode(heard):
    client = make_client(write_without_response=True)
    record_attempts(client)
    asyncio.run(client.connect())

    state = client.debug_state()
    assert state["write_without_response"] is True
    assert 0 < state["next_reconnect_in"] <= const.RECONNECT_BACKOFF_MIN


# --------------------------------------------------------------------------
# Write mode
# --------------------------------------------------------------------------

class RecordingBle:
    def __init__(self):
        self.is_connected = True
        self.calls: list[dict] = []

    async def write_gatt_char(self, char, data, **kwargs):
        self.calls.append({"char": char, **kwargs})

    async def disconnect(self):
        self.is_connected = False


def _connected(client):
    ble = RecordingBle()
    client._client = ble
    client._sk, client._macdata = list(range(16)), [1, 2, 3, 4, 5, 6]
    client._connected = True
    return ble


@pytest.mark.parametrize(("without_response", "expected"), [(False, True), (True, False)])
def test_control_writes_follow_the_write_mode(monkeypatch, without_response, expected):
    monkeypatch.setattr(cm, "WRITE_MIN_GAP", 0)
    monkeypatch.setattr(cm, "WRITE_MIN_GAP_NO_RESPONSE", 0)
    client = make_client(write_without_response=without_response)
    ble = _connected(client)

    assert asyncio.run(client.set_power(5, True))
    assert ble.calls == [{"char": const.CYNC_CONTROL_CHAR, "response": expected}]


def test_no_response_mode_uses_the_wider_gap(monkeypatch):
    monkeypatch.setattr(cm, "WRITE_MIN_GAP", 0)
    monkeypatch.setattr(cm, "WRITE_MIN_GAP_NO_RESPONSE", 0.03)
    client = make_client(write_without_response=True)
    ble = _connected(client)
    times = []

    async def timed_write(char, data, **kwargs):
        times.append(asyncio.get_running_loop().time())

    ble.write_gatt_char = timed_write

    async def run():
        await client.set_power(5, True)
        await client.set_brightness(5, 40)

    asyncio.run(run())
    assert times[1] - times[0] >= 0.03 * 0.9


# --------------------------------------------------------------------------
# Options flow
# --------------------------------------------------------------------------

class _Flow(config_flow.CyncBLEOptionsFlow):
    """The HA OptionsFlow base is stubbed as object; supply its two methods."""

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}


def _entry(options):
    return types.SimpleNamespace(options=options)


def test_options_form_defaults_to_the_stored_value():
    flow = _Flow(_entry({const.CONF_WRITE_WITHOUT_RESPONSE: True}))
    result = asyncio.run(flow.async_step_init())

    assert result["step_id"] == "init"
    (marker,) = result["data_schema"]
    assert marker.key == const.CONF_WRITE_WITHOUT_RESPONSE
    assert marker.default is True


def test_options_form_defaults_to_off():
    flow = _Flow(_entry({}))
    (marker,) = asyncio.run(flow.async_step_init())["data_schema"]
    assert marker.default is False


def test_options_submit_saves_the_choice():
    flow = _Flow(_entry({}))
    result = asyncio.run(flow.async_step_init({const.CONF_WRITE_WITHOUT_RESPONSE: True}))

    assert result["type"] == "create_entry"
    assert result["data"] == {const.CONF_WRITE_WITHOUT_RESPONSE: True}
