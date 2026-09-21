"""Tests for the system-status snapshot and its diagnostic entities.

A real coordinator is constructed over a two-mesh device set and driven
through the states that matter, rather than asserting against a mocked
snapshot — the invariant worth protecting is that the summary counts and the
detail lists come from the same pass and cannot disagree.
"""
from __future__ import annotations

import datetime
import time
import types

import pytest
from conftest import load

co = load("coordinator")
se = load("sensor")
bs = load("binary_sensor")
const = load("const")

MESH_A = "AABBCCDDEE00"
MESH_B = "1122334455FF"


def _hass():
    return types.SimpleNamespace(async_create_task=lambda coro: None)


def cloud_device(device_id, name, mesh=MESH_A, mac=None):
    return {
        "device_id": device_id,
        "name": name,
        "mesh_name": mesh,
        "mac": mac or f"AA:BB:CC:DD:EE:{device_id:02X}",
        "access_key": "key",
    }


@pytest.fixture
def coord():
    """Coordinator with three devices on mesh A and one on mesh B."""
    config = [
        cloud_device(1, "Lamp A"),
        cloud_device(2, "Lamp B"),
        cloud_device(3, "Lamp C"),
        cloud_device(4, "Lamp D", mesh=MESH_B, mac="11:22:33:44:55:04"),
    ]
    return co.CyncBLECoordinator(hass=_hass(), devices_config=config,
                                 entry_id="entry-1")


def connect(client):
    client._connected = True
    client._sk = [0] * 16
    client._current_mac = "AA:BB:CC:DD:EE:01"


def disconnect(client, seconds_ago=999):
    """Drop a client past the reconnect grace period."""
    client._connected = False
    client._disconnected_at = time.monotonic() - seconds_ago


# --------------------------------------------------------------------------
# Baseline
# --------------------------------------------------------------------------

def test_cold_start_reports_everything_as_never_reported(coord):
    status = coord.system_status()
    assert status["meshes"]["total"] == 2
    assert status["meshes"]["connected"] == 0
    assert status["devices"]["total"] == 4
    assert status["devices"]["available"] == 0
    assert status["devices"]["unavailable_by_reason"] == {"never_reported": 4}
    assert status["last_activity"] is None
    assert status["firmware"]["known"] == 0


def test_counts_agree_with_their_own_detail_lists(coord):
    """The reason all metrics come from one pass in system_status()."""
    connect(coord._mesh_clients[MESH_A])
    coord._devices[f"{MESH_A}/1"].mark_seen()
    status = coord.system_status()

    detail = status["devices"]["unavailable_detail"]
    assert status["devices"]["unavailable"] == sum(len(v) for v in detail.values())
    assert status["devices"]["unavailable_by_reason"] == \
        {k: len(v) for k, v in detail.items()}


def test_empty_config_does_not_crash():
    coord = co.CyncBLECoordinator(hass=_hass(), devices_config=[], entry_id="e")
    status = coord.system_status()
    assert (status["devices"]["total"], status["meshes"]["total"]) == (0, 0)
    assert status["last_activity"] is None


# --------------------------------------------------------------------------
# The three unavailability reasons
# --------------------------------------------------------------------------

def test_reporting_devices_become_available(coord):
    connect(coord._mesh_clients[MESH_A])
    for i in (1, 2, 3):
        coord._devices[f"{MESH_A}/{i}"].mark_seen()

    status = coord.system_status()
    assert status["meshes"]["connected"] == 1
    assert status["devices"]["available"] == 3
    assert status["devices"]["unavailable_by_reason"] == {"never_reported": 1}
    assert status["meshes"]["detail"][MESH_A]["active_mac"] == "AA:BB:CC:DD:EE:01"
    assert status["last_activity"] is not None


def test_the_three_reasons_are_kept_apart(coord):
    """They call for different fixes, so collapsing them loses the point."""
    connect(coord._mesh_clients[MESH_A])
    for i in (1, 2, 3):
        coord._devices[f"{MESH_A}/{i}"].mark_seen()

    coord._devices[f"{MESH_A}/1"]._confirmed_unreachable = True
    disconnect(coord._mesh_clients[MESH_A])
    connect(coord._mesh_clients[MESH_B])
    coord._devices[f"{MESH_B}/4"].mark_seen()

    status = coord.system_status()
    assert status["devices"]["unavailable_by_reason"] == \
        {"probe_failed": 1, "mesh_disconnected": 2}
    assert status["devices"]["unavailable_detail"]["probe_failed"] == ["Lamp A"]
    assert status["devices"]["available"] == 1


def test_probe_failure_takes_precedence_over_a_downed_mesh(coord):
    """A device we proved unreachable stays reported that way."""
    device = coord._devices[f"{MESH_A}/1"]
    device.mark_seen()
    device._confirmed_unreachable = True
    disconnect(coord._mesh_clients[MESH_A])
    assert device.unavailable_reason == "probe_failed"


def test_a_healthy_device_has_no_unavailable_reason(coord):
    connect(coord._mesh_clients[MESH_A])
    device = coord._devices[f"{MESH_A}/1"]
    device.mark_seen()
    assert device.unavailable_reason is None


# --------------------------------------------------------------------------
# Quiet devices
# --------------------------------------------------------------------------

def test_only_devices_past_the_quiet_threshold_are_listed(coord):
    connect(coord._mesh_clients[MESH_A])
    for i in (2, 3):
        coord._devices[f"{MESH_A}/{i}"].mark_seen()

    coord._devices[f"{MESH_A}/2"].last_seen = \
        time.monotonic() - (const.PROBE_QUIET_THRESHOLD + 30)
    coord._devices[f"{MESH_A}/3"].last_seen = time.monotonic() - 5

    quiet = coord.system_status()["devices"]["quiet"]
    assert [q["name"] for q in quiet] == ["Lamp B"]
    assert quiet[0]["seconds_since_seen"] > const.PROBE_QUIET_THRESHOLD
    assert "probe_misses" in quiet[0]


def test_quiet_devices_are_sorted_quietest_first(coord):
    connect(coord._mesh_clients[MESH_A])
    for i in (2, 3):
        coord._devices[f"{MESH_A}/{i}"].mark_seen()
    coord._devices[f"{MESH_A}/2"].last_seen = \
        time.monotonic() - (const.PROBE_QUIET_THRESHOLD + 30)
    coord._devices[f"{MESH_A}/3"].last_seen = \
        time.monotonic() - (const.PROBE_QUIET_THRESHOLD + 300)

    quiet = coord.system_status()["devices"]["quiet"]
    assert [q["name"] for q in quiet] == ["Lamp C", "Lamp B"]


# --------------------------------------------------------------------------
# Mesh connection health
# --------------------------------------------------------------------------

def test_mac_cooldown_is_surfaced(coord):
    """This bookkeeping existed but was previously invisible."""
    client = coord._mesh_clients[MESH_A]
    for _ in range(const.MAC_FAIL_THRESHOLD):
        client._record_mac_failure("AA:BB:CC:DD:EE:07")

    detail = coord.system_status()["meshes"]["detail"][MESH_A]
    assert list(detail["macs_in_cooldown"]) == ["AA:BB:CC:DD:EE:07"]
    assert detail["macs_in_cooldown"]["AA:BB:CC:DD:EE:07"] > 0
    assert detail["mac_failure_counts"]["AA:BB:CC:DD:EE:07"] == \
        const.MAC_FAIL_THRESHOLD


def test_active_mac_is_none_while_disconnected(coord):
    client = coord._mesh_clients[MESH_A]
    connect(client)
    assert client.current_mac is not None
    disconnect(client)
    assert client.current_mac is None


# --------------------------------------------------------------------------
# Firmware and unknown devices
# --------------------------------------------------------------------------

def test_firmware_counts_undecodable_replies_as_known(coord):
    """An undecodable payload is still a reply — the raw bytes are the record."""
    coord._devices[f"{MESH_A}/2"].update_from_version("1.2", "01020000")
    coord._devices[f"{MESH_A}/3"].update_from_version(None, "abcd0000")

    firmware = coord.system_status()["firmware"]
    assert firmware["known"] == 2
    assert firmware["versions"]["Lamp C"] is None
    assert firmware["total"] == 4


def test_unknown_device_keys_are_reported_and_sorted(coord):
    coord._unknown_device_keys.update({f"{MESH_A}/9", f"{MESH_A}/8"})
    unknown = coord.system_status()["unknown_devices"]
    assert unknown["count"] == 2
    assert unknown["keys"] == [f"{MESH_A}/8", f"{MESH_A}/9"]


def test_answering_a_version_query_marks_the_device_seen(coord):
    """A directed reply is liveness evidence, like a probe reply."""
    device = coord._devices[f"{MESH_A}/1"]
    device._probe_miss_count = 1
    device._confirmed_unreachable = True

    device.update_from_version("1.2", "01020000")
    assert device.last_seen is not None
    assert device.probe_miss_count == 0
    assert device._confirmed_unreachable is False


# --------------------------------------------------------------------------
# Snapshot memoisation
# --------------------------------------------------------------------------

def test_snapshot_is_reused_within_the_cache_window(coord):
    """One listener dispatch reads this 21 times inside a single synchronous
    block; they must not each recompute it."""
    first = coord.system_status()
    assert coord.system_status() is first


def test_snapshot_is_recomputed_after_the_window_expires(coord):
    coord.system_status()
    # Age the cache rather than sleeping.
    coord._status_cache_at -= const.STATUS_CACHE_TTL * 2
    connect(coord._mesh_clients[MESH_A])
    assert coord.system_status()["meshes"]["connected"] == 1


def test_the_cache_window_is_far_shorter_than_the_poll_interval(coord):
    """The staleness this introduces must stay negligible against the
    interval the integration already tolerates for connection state."""
    assert const.STATUS_CACHE_TTL < const.POLL_INTERVAL / 100


def test_connectivity_binary_sensor_does_not_read_through_the_cache(coord):
    """is_on is the one signal worth having live, so it reads
    connected_mesh_count directly rather than the memoised snapshot."""
    sensor = bs.CyncBLEMeshConnectivitySensor(coord)
    coord.system_status()          # prime the cache while disconnected
    connect(coord._mesh_clients[MESH_A])
    assert sensor.is_on is True    # reflects reality immediately


# --------------------------------------------------------------------------
# Entities
# --------------------------------------------------------------------------

def all_sensors(coordinator):
    return [
        se.CyncBLEConnectedMeshesSensor(coordinator),
        se.CyncBLEAvailableDevicesSensor(coordinator),
        se.CyncBLEUnavailableDevicesSensor(coordinator),
        se.CyncBLEQuietDevicesSensor(coordinator),
        se.CyncBLEUnknownDevicesSensor(coordinator),
        se.CyncBLEFirmwareKnownSensor(coordinator),
        se.CyncBLELastActivitySensor(coordinator),
    ]


def test_sensors_report_the_snapshot_values(coord):
    connect(coord._mesh_clients[MESH_A])
    for i in (1, 2, 3):
        coord._devices[f"{MESH_A}/{i}"].mark_seen()
    coord._devices[f"{MESH_A}/2"].update_from_version("1.2", "01020000")
    coord._unknown_device_keys.add(f"{MESH_A}/9")

    by_key = {s._key: s for s in all_sensors(coord)}
    status = coord.system_status()

    assert by_key["connected_meshes"].native_value == status["meshes"]["connected"]
    assert by_key["available_devices"].native_value == status["devices"]["available"]
    assert by_key["unavailable_devices"].native_value == \
        status["devices"]["unavailable"]
    assert by_key["quiet_devices"].native_value == len(status["devices"]["quiet"])
    assert by_key["unknown_devices"].native_value == 1
    assert by_key["firmware_known"].native_value == 1
    assert isinstance(by_key["last_activity"].native_value, datetime.datetime)


def test_binary_sensor_is_on_when_any_mesh_is_connected(coord):
    """"Any", not "all": one connected mesh still controls its own bulbs."""
    sensor = bs.CyncBLEMeshConnectivitySensor(coord)
    assert sensor.is_on is False
    connect(coord._mesh_clients[MESH_A])
    assert sensor.is_on is True


def test_diagnostic_entities_stay_available_during_a_full_outage(coord):
    """A diagnostic entity that goes unavailable is useless exactly when
    it's needed."""
    for client in coord._mesh_clients.values():
        disconnect(client)

    sensors = all_sensors(coord)
    binary = bs.CyncBLEMeshConnectivitySensor(coord)

    assert all(s.available for s in sensors)
    assert binary.available is True
    assert binary.is_on is False
    # Reports zero, not "unknown".
    by_key = {s._key: s for s in sensors}
    assert by_key["connected_meshes"].native_value == 0
    assert by_key["available_devices"].native_value == 0


def test_entities_form_one_service_device_scoped_to_the_entry(coord):
    info = bs.CyncBLEMeshConnectivitySensor(coord).device_info
    assert info["identifiers"] == {(const.DOMAIN, "system_entry-1")}
    assert info["entry_type"] == "service"


def test_unique_ids_are_distinct_and_entry_scoped(coord):
    entities = all_sensors(coord) + [bs.CyncBLEMeshConnectivitySensor(coord)]
    unique_ids = {e._attr_unique_id for e in entities}
    assert len(unique_ids) == len(entities)
    assert "cync_ble_system_entry-1_connected_meshes" in unique_ids


def test_a_second_entry_gets_its_own_service_device():
    """Two Cync accounts must not collide on one system device."""
    other = co.CyncBLECoordinator(hass=_hass(), devices_config=[],
                                  entry_id="entry-2")
    info = bs.CyncBLEMeshConnectivitySensor(other).device_info
    assert info["identifiers"] == {(const.DOMAIN, "system_entry-2")}


def test_entities_are_categorised_as_diagnostic(coord):
    entities = all_sensors(coord) + [bs.CyncBLEMeshConnectivitySensor(coord)]
    assert all(e._attr_entity_category == "diagnostic" for e in entities)
