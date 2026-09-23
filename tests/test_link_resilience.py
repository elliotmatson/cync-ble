"""How the mesh client behaves when the single GATT link is slow or wedged.

The failure these guard against, from a real log: Adaptive Lighting sends
brightness and colour to ~20 bulbs at once, every write is serialised onto
the one GATT session, the first write times out, and every write queued
behind it then times out too, one BLE_TIMEOUT apart, until the peripheral
finally drops and the whole mesh goes with it. Each queued command had its
own fresh retry budget, so nothing ever concluded the link was wedged.

These tests drive the real send_packet against a fake BLE client whose
writes can be made to hang or fail, so what they assert is the actual
locking, counting and reset behaviour rather than a mock of it.
"""
from __future__ import annotations

import asyncio

import pytest
from conftest import load

cm = load("cync_mesh")
const = load("const")
coordinator_mod = load("coordinator")

SK = list(range(16))
MACDATA = [0x66, 0x55, 0x44, 0x33, 0x22, 0x11]


class FakeBleClient:
    """Stands in for a connected BleakClient.

    hang=True makes every write block forever (until timed out by
    _write_gatt), like a write whose ATT request the proxy never answers.
    """

    def __init__(self, *, hang: bool = False) -> None:
        self.hang = hang
        self.is_connected = True
        self.writes: list[bytes] = []
        self.write_times: list[float] = []
        self.disconnects = 0

    async def write_gatt_char(self, char, data, **kwargs):
        if self.hang:
            await asyncio.Event().wait()
        loop = asyncio.get_running_loop()
        self.writes.append(bytes(data))
        self.write_times.append(loop.time())

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False


def make_client(ble: FakeBleClient) -> "cm.CyncMeshClient":
    client = cm.CyncMeshClient(
        hass=None, mesh_name="112233445566", mesh_password="pw",
        mesh_macs=["11:22:33:44:55:66"],
    )
    client._client = ble
    client._sk, client._macdata = SK, MACDATA
    client._connected = True
    return client


@pytest.fixture(autouse=True)
def fast_timeouts(monkeypatch):
    """Shrink BLE_TIMEOUT and write pacing so the suite stays fast.

    Patched on cync_mesh, not const: the module imports them by name.
    """
    monkeypatch.setattr(cm, "BLE_TIMEOUT", 0.05)
    monkeypatch.setattr(cm, "WRITE_MIN_GAP", 0)


async def _no_reconnect(*args, **kwargs):
    return False


# --------------------------------------------------------------------------
# Stall detection across callers
# --------------------------------------------------------------------------

def test_single_timeout_keeps_the_session():
    """One slow write is tolerated — tearing down on every timeout is what
    the Sept 8 change fixed, and that part should stay fixed."""
    ble = FakeBleClient(hang=True)
    client = make_client(ble)

    ok = asyncio.run(client.send_packet(5, const.CMD_POWER, [1], allow_reconnect=False))

    assert ok is False
    assert client.is_connected
    assert ble.disconnects == 0
    assert client._consecutive_write_failures == 1


def test_second_consecutive_timeout_from_another_caller_resets():
    """The counter spans callers. Two separate probe-style sends, each with
    only one attempt of its own, still add up to a wedged link."""
    ble = FakeBleClient(hang=True)
    client = make_client(ble)

    async def run():
        await client.send_packet(5, const.CMD_STATUS_QUERY, [0x10], allow_reconnect=False)
        assert client.is_connected
        await client.send_packet(6, const.CMD_STATUS_QUERY, [0x10], allow_reconnect=False)

    asyncio.run(run())

    assert not client.is_connected
    assert ble.disconnects == 1


def test_success_clears_the_failure_count():
    ble = FakeBleClient(hang=True)
    client = make_client(ble)

    async def run():
        await client.send_packet(5, const.CMD_POWER, [1], allow_reconnect=False)
        ble.hang = False
        assert await client.send_packet(5, const.CMD_POWER, [1], allow_reconnect=False)
        ble.hang = True
        await client.send_packet(5, const.CMD_POWER, [1], allow_reconnect=False)

    asyncio.run(run())

    # fail, succeed, fail — never two in a row, so never reset
    assert client.is_connected
    assert ble.disconnects == 0


def test_queued_commands_do_not_each_wait_out_a_timeout():
    """The regression itself: many commands queued behind a wedged write.

    Before, each one timed out in turn (N x BLE_TIMEOUT). Now the second
    failure resets the link and the rest fail fast instead of each hanging.
    """
    ble = FakeBleClient(hang=True)
    client = make_client(ble)
    client.connect = _no_reconnect  # the proxy is congested; reconnect fails

    async def run():
        loop = asyncio.get_running_loop()
        started = loop.time()
        results = await asyncio.gather(
            *(client.set_brightness(device_id, 50) for device_id in range(10))
        )
        return results, loop.time() - started

    results, elapsed = asyncio.run(run())

    assert results == [False] * 10
    # Two timeouts to detect the stall; the old behaviour was ~20 of them.
    assert elapsed < cm.BLE_TIMEOUT * 5
    assert ble.disconnects == 1


def test_failure_on_a_replaced_session_does_not_reset_the_new_one():
    """A slow failure on an old client must not tear down a fresh session
    another caller has already established."""
    old = FakeBleClient()
    new = FakeBleClient()
    client = make_client(new)
    client._consecutive_write_failures = const.LINK_STALL_THRESHOLD - 1

    asyncio.run(client._handle_write_failure(old))

    assert client.is_connected
    assert client._client is new
    assert new.disconnects == 0
    assert client._consecutive_write_failures == const.LINK_STALL_THRESHOLD - 1


def test_dead_link_is_reset_on_the_first_failure():
    ble = FakeBleClient()
    client = make_client(ble)
    ble.is_connected = False

    asyncio.run(client._handle_write_failure(ble))

    assert not client.is_connected


# --------------------------------------------------------------------------
# Latest-wins coalescing
# --------------------------------------------------------------------------

def _opcodes_and_targets(client, ble):
    """Decrypt what was written back into (target, opcode, first param)."""
    out = []
    for wire in ble.writes:
        # _encrypt_packet encrypts bytes 5.. with a keystream that depends
        # only on the packet header, so re-encrypting a zeroed body with the
        # same header recovers the keystream.
        header = list(wire[:3]) + [0] * 17
        keystream = cm._encrypt_packet(SK, MACDATA, header)
        body = [wire[i] ^ keystream[i] for i in range(5, 20)]
        out.append((body[0] | (body[1] << 8), body[2], body[5]))
    return out


def test_superseded_commands_for_one_light_are_dropped():
    """Three colour-temp updates queued for the same bulb: only the newest
    is written, and the dropped ones still report success to the caller."""
    ble = FakeBleClient()
    client = make_client(ble)

    async def run():
        async with client._write_lock:  # hold the link so all three queue
            tasks = [
                asyncio.create_task(client.set_color_temp(5, value))
                for value in (10, 20, 30)
            ]
            await asyncio.sleep(0)
        return await asyncio.gather(*tasks)

    results = asyncio.run(run())

    assert results == [True, True, True]
    assert len(ble.writes) == 1
    target, opcode, _ = _opcodes_and_targets(client, ble)[0]
    assert (target, opcode) == (5, const.CMD_COLOR)


def test_coalescing_is_per_light_and_per_opcode():
    """Different bulbs, and different opcodes on one bulb, are all sent —
    a turn-on's power + brightness + colour must never collapse."""
    ble = FakeBleClient()
    client = make_client(ble)

    async def run():
        async with client._write_lock:
            tasks = [
                asyncio.create_task(client.set_power(5, True)),
                asyncio.create_task(client.set_brightness(5, 40)),
                asyncio.create_task(client.set_color_temp(5, 20)),
                asyncio.create_task(client.set_brightness(6, 40)),
            ]
            await asyncio.sleep(0)
        return await asyncio.gather(*tasks)

    assert asyncio.run(run()) == [True] * 4
    sent = [(t, op) for t, op, _ in _opcodes_and_targets(client, ble)]
    assert sent == [
        (5, const.CMD_POWER),
        (5, const.CMD_BRIGHTNESS),
        (5, const.CMD_COLOR),
        (6, const.CMD_BRIGHTNESS),
    ]


def test_probes_and_broadcasts_are_never_coalesced():
    ble = FakeBleClient()
    client = make_client(ble)

    async def run():
        async with client._write_lock:
            tasks = [
                asyncio.create_task(client.request_status()),
                asyncio.create_task(client.request_status()),
            ]
            await asyncio.sleep(0)
        return await asyncio.gather(*tasks)

    assert asyncio.run(run()) == [True, True]
    assert len(ble.writes) == 2


def test_writes_are_paced(monkeypatch):
    monkeypatch.setattr(cm, "WRITE_MIN_GAP", 0.03)
    ble = FakeBleClient()
    client = make_client(ble)

    async def run():
        await client.set_power(5, True)
        await client.set_brightness(5, 40)

    asyncio.run(run())

    assert len(ble.write_times) == 2
    assert ble.write_times[1] - ble.write_times[0] >= 0.03 * 0.9


# --------------------------------------------------------------------------
# Probes hold off while commands are in flight
# --------------------------------------------------------------------------

def test_is_busy_during_and_shortly_after_a_command(monkeypatch):
    ble = FakeBleClient()
    client = make_client(ble)
    assert not client.is_busy

    seen_busy = []

    async def recording_write(char, data, **kwargs):
        seen_busy.append(client.is_busy)

    ble.write_gatt_char = recording_write
    asyncio.run(client.set_power(5, True))

    assert seen_busy == [True]
    assert client.is_busy  # within COMMAND_QUIET_PERIOD
    client._last_command_at -= const.COMMAND_QUIET_PERIOD + 1
    assert not client.is_busy


def test_probe_is_deferred_while_busy_and_runs_once_idle():
    class ProbeMesh:
        is_connected = True
        recently_disconnected = False
        is_busy = True

        def __init__(self):
            self.probes = 0

        async def query_device_status(self, device_id):
            self.probes += 1
            return True

    mesh = ProbeMesh()
    device = coordinator_mod.CyncBLEDevice(
        {"device_id": 5, "name": "Lamp", "mesh_name": "112233445566"}, mesh,
    )
    device.last_seen = 0.0  # long quiet — due for a probe

    asyncio.run(device.probe_if_quiet())
    assert mesh.probes == 0
    # Deferral must not burn the PROBE_INTERVAL slot.
    assert device._last_probe_attempt is None

    mesh.is_busy = False
    asyncio.run(device.probe_if_quiet())
    assert mesh.probes == 1


# --------------------------------------------------------------------------
# Disconnect callback identity
# --------------------------------------------------------------------------

def test_stale_client_disconnect_does_not_drop_the_live_session():
    live = FakeBleClient()
    stale = FakeBleClient()
    client = make_client(live)

    client._on_disconnected(stale)

    assert client.is_connected
    assert client._client is live


def test_current_client_disconnect_resets():
    live = FakeBleClient()
    client = make_client(live)
    live.is_connected = False

    client._on_disconnected(live)

    assert not client.is_connected
    assert client._client is None


def test_disconnect_with_mismatched_identity_still_resets_a_dead_link():
    """If HA's wrapper ever hands the callback a different object for our
    own client, a genuine drop must still be honoured."""
    live = FakeBleClient()
    client = make_client(live)
    live.is_connected = False

    client._on_disconnected(object())

    assert not client.is_connected
