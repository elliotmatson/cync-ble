"""How many mesh writes a single light command costs.

The Telink protocol has no combined command: power (0xD0), luminance (0xD2)
and colour/CT (0xE2) are separate opcodes, and send_packet serialises them
on _write_lock. So a turn-on carrying brightness and colour is three
sequential BLE round-trips, which on a proxied mesh is visible as the bulb
coming on at its previous state and then correcting itself.

These tests pin that write count. They are not asserting that three writes
is desirable — they are recording what it currently is, so that if someone
reduces it (for example by dropping the redundant power write) the change is
visible here rather than discovered on a physical light.
"""
from __future__ import annotations

import asyncio

import pytest
from conftest import load

light = load("light")
const = load("const")


class RecordingMesh:
    """Mesh client that records send_packet calls instead of writing."""

    def __init__(self) -> None:
        self.writes: list[tuple[int, int]] = []
        self.is_connected = True
        self.recently_disconnected = False

    async def _record(self, device_id, command):
        self.writes.append((device_id, command))
        return True

    async def set_power(self, device_id, on):
        return await self._record(device_id, const.CMD_POWER)

    async def set_brightness(self, device_id, brightness):
        return await self._record(device_id, const.CMD_BRIGHTNESS)

    async def set_color_temp(self, device_id, value):
        return await self._record(device_id, const.CMD_COLOR)

    async def set_rgb(self, device_id, r, g, b):
        return await self._record(device_id, const.CMD_COLOR)

    @property
    def opcodes(self):
        return [op for _, op in self.writes]


@pytest.fixture
def entity():
    """A light entity backed by a recording mesh client."""
    coordinator_mod = load("coordinator")
    mesh = RecordingMesh()
    device = coordinator_mod.CyncBLEDevice(
        {"device_id": 5, "name": "Lamp", "mac": "AA:BB:CC:DD:EE:01",
         "mesh_name": "AABBCCDDEE00", "supports_rgb": True,
         "supports_temperature": True},
        mesh,
    )
    ent = light.CyncBLELight(coordinator=None, device=device)
    return ent, mesh, device


def test_bare_turn_on_is_a_single_write(entity):
    ent, mesh, _ = entity
    asyncio.run(ent.async_turn_on())
    assert mesh.opcodes == [const.CMD_POWER]


def test_turn_on_with_brightness_is_two_writes(entity):
    ent, mesh, _ = entity
    asyncio.run(ent.async_turn_on(brightness=128))
    assert mesh.opcodes == [const.CMD_POWER, const.CMD_BRIGHTNESS]


def test_turn_on_with_brightness_and_color_temp_is_three_writes(entity):
    """The case behind the visible stepping: on, then brightness, then CT."""
    ent, mesh, _ = entity
    asyncio.run(ent.async_turn_on(brightness=128, color_temp_kelvin=4000))
    assert mesh.opcodes == [
        const.CMD_POWER, const.CMD_BRIGHTNESS, const.CMD_COLOR,
    ]


def test_turn_on_with_brightness_and_rgb_is_three_writes(entity):
    ent, mesh, _ = entity
    asyncio.run(ent.async_turn_on(brightness=200, hs_color=(120, 100)))
    assert mesh.opcodes == [
        const.CMD_POWER, const.CMD_BRIGHTNESS, const.CMD_COLOR,
    ]


def test_power_write_is_skipped_when_already_on(entity):
    """Adjusting an already-on light costs one write, not two."""
    ent, mesh, device = entity
    device._is_on = True
    asyncio.run(ent.async_turn_on(brightness=128))
    assert mesh.opcodes == [const.CMD_BRIGHTNESS]


def test_a_failed_power_write_aborts_the_remaining_writes(entity):
    """No point sending attributes to a light that didn't come on."""
    ent, mesh, _ = entity

    async def fail(device_id, on):
        return False

    mesh.set_power = fail
    asyncio.run(ent.async_turn_on(brightness=128, color_temp_kelvin=4000))
    assert mesh.opcodes == []


def test_turn_off_is_a_single_write(entity):
    ent, mesh, device = entity
    device._is_on = True
    asyncio.run(ent.async_turn_off())
    assert mesh.opcodes == [const.CMD_POWER]
