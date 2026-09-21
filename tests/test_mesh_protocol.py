"""Tests for the Telink mesh packet layer.

Synthetic notification frames are run through the real _on_notification, so
these cover the decrypt path, opcode dispatch and payload parsing together
rather than testing the decoders in isolation.

Wire-format note: the notify format is NOT the inverse of _encrypt_packet.
_decrypt_packet XORs bytes 7+ with a keystream derived from bytes 0-4, which
it leaves untouched, so it is its own inverse — running plaintext through it
once yields exactly the ciphertext the handler will turn back into that
plaintext.
"""
from __future__ import annotations

import asyncio

import pytest
from conftest import load

cm = load("cync_mesh")
const = load("const")

SK = list(range(16))
MESH_MAC = "AA:BB:CC:DD:EE:04"
MACDATA = [int(p, 16) for p in reversed(MESH_MAC.split(":"))]


def make_wire(src: int, cmd: int, params: list[int]) -> bytearray:
    """Build an on-the-wire notification frame for one device."""
    pkt = [0] * 20
    pkt[0], pkt[1], pkt[2] = 0x11, 0x11, 0x88      # sequence number
    pkt[3] = src & 0xFF                             # source addr, little-endian
    pkt[4] = (src >> 8) & 0xFF
    pkt[5], pkt[6] = 0x00, 0x00                     # crypto check field
    pkt[7] = cmd
    pkt[8], pkt[9] = 0x11, 0x02                     # VendorID 0x0211
    for i, byte in enumerate(params):
        pkt[10 + i] = byte
    return bytearray(cm._decrypt_packet(SK, MACDATA, list(pkt)))


def new_client(**kwargs):
    client = cm.CyncMeshClient(
        hass=None, mesh_name=MESH_MAC, mesh_password="pw",
        mesh_macs=[MESH_MAC], **kwargs,
    )
    client._sk, client._macdata = SK, MACDATA
    return client


def collector():
    """Return (list, async callback appending to it)."""
    seen: list = []

    async def callback(item):
        seen.append(item)

    return seen, callback


# --------------------------------------------------------------------------
# Wire format
# --------------------------------------------------------------------------

def test_decrypt_packet_is_its_own_inverse():
    plain = [0x11, 0x11, 0x88, 0x2A, 0x00, 0, 0, 0xC8, 0x11, 0x02] + \
            [0x56, 0x31, 0x2E, 0x32] + [0] * 6
    wire = cm._decrypt_packet(SK, MACDATA, list(plain))
    assert cm._decrypt_packet(SK, MACDATA, list(wire)) == plain


def test_decrypt_packet_leaves_the_header_bytes_untouched():
    plain = [0x11, 0x11, 0x88, 0x2A, 0x00] + [0] * 15
    assert cm._decrypt_packet(SK, MACDATA, list(plain))[:5] == plain[:5]


# --------------------------------------------------------------------------
# Firmware version decoding
#
# The reply opcode and payload layout are INFERRED, not documented — see
# const.CMD_MESH_OTA_READ_RSP. These tests pin the "refuse to guess"
# behaviour that caveat demands.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("params", "expected"),
    [
        (list(b"V1.2") + [0x00] * 6, "V1.2"),
        (list(b"V1.2") + [0xFF] * 6, "V1.2"),
        (list(b" V1.2 ") + [0x00] * 4, "V1.2"),
        ([0x01, 0x02] + [0x00] * 8, "1.2"),
        ([0x02, 0x0A] + [0xFF] * 8, "2.10"),
        ([0x01, 0x0A] + [0x00] * 8, "1.10"),
        ([0x01, 0x02, 0x03] + [0x00] * 7, "1.2.3"),
    ],
    ids=["ascii-nul-pad", "ascii-ff-pad", "ascii-spaces", "packed-2",
         "packed-with-10", "packed-0x0a-not-stripped", "packed-3"],
)
def test_recognised_version_shapes_decode(params, expected):
    assert cm.decode_firmware_version(params) == expected


@pytest.mark.parametrize(
    "params",
    [
        [0x00] * 10,
        [0xFF] * 10,
        [0x20, 0x20, 0x20] + [0x00] * 7,
        [0x20, 0x20, 0x09] + [0x00] * 7,
        [0xAB, 0xCD, 0x01] + [0x00] * 7,
        [1, 2, 3, 4, 5] + [0x00] * 5,
    ],
    ids=["all-nul", "all-ff", "all-spaces", "spaces-and-tab",
         "binary-garbage", "too-many-components"],
)
def test_unrecognised_payloads_return_none_rather_than_guessing(params):
    """A wrong opcode inference hands us arbitrary bytes. Those must read as
    unknown, not as a plausible-looking version.

    spaces-and-tab is the regression case: classifying "printable" as
    0x20-0x7E only sent it to the packed branch, where it rendered as
    "32.32.9" — a version invented out of padding.
    """
    assert cm.decode_firmware_version(params) is None


def test_decoder_does_not_mutate_the_callers_list():
    """Callers pass a slice of the live decrypted packet."""
    params = [0x56, 0x31, 0x2E, 0x32, 0x00, 0xFF, 0, 0, 0, 0]
    snapshot = list(params)
    cm.decode_firmware_version(params)
    assert params == snapshot


# --------------------------------------------------------------------------
# Version reply dispatch
# --------------------------------------------------------------------------

@pytest.mark.parametrize("src", [1, 42, 255, 0x0111])
def test_source_device_id_is_read_little_endian(src):
    """Same field the Device_Addr 0xE1 notify uses — AN-BLE-15120202-E3
    §2.13 Table 14 shows 0x0011 on the wire as `11 00`."""
    seen, callback = collector()
    client = new_client(version_callback=callback)
    asyncio.run(client._on_notification(
        None, make_wire(src, const.CMD_MESH_OTA_READ_RSP, [0x01, 0x02])))
    assert seen[0].device_id == src


def test_raw_bytes_are_always_retained_even_when_undecodable():
    """The raw hex is the evidence for confirming the inferred layout."""
    seen, callback = collector()
    client = new_client(version_callback=callback)
    params = [0xAB, 0xCD] + [0x00] * 8
    asyncio.run(client._on_notification(
        None, make_wire(7, const.CMD_MESH_OTA_READ_RSP, params)))
    assert seen[0].version is None
    assert seen[0].raw == bytes(params).hex()


def test_version_callback_is_optional():
    client = new_client()
    asyncio.run(client._on_notification(
        None, make_wire(3, const.CMD_MESH_OTA_READ_RSP, [0x01, 0x02])))


def test_a_raising_version_callback_is_contained():
    """A callback error must not propagate into the BLE notification handler."""
    async def boom(_):
        raise RuntimeError("boom")

    client = new_client(version_callback=boom)
    asyncio.run(client._on_notification(
        None, make_wire(3, const.CMD_MESH_OTA_READ_RSP, [0x01, 0x02])))


# --------------------------------------------------------------------------
# Regression: the pre-existing status paths still work
# --------------------------------------------------------------------------

def test_status_broadcast_decodes_both_device_slots():
    seen, callback = collector()
    client = new_client(status_callback=callback)
    params = [0x05, 0x01, 0x32, 0x28, 0x09, 0x01, 0xC8, 0xE0, 0x00, 0x00]
    asyncio.run(client._on_notification(
        None, make_wire(0, const.CMD_STATUS_RESPONSE, params)))

    assert len(seen) == 2
    first, second = seen
    assert (first.device_id, first.brightness, first.is_rgb, first.color_temp) == \
        (5, 0x32, False, 0x28)
    assert (second.device_id, second.is_rgb) == (9, True)
    assert second.brightness == 0xC8 - 128
    assert (second.red, second.green, second.blue) == (255, 0, 0)


def test_offline_device_slot_is_skipped():
    seen, callback = collector()
    client = new_client(status_callback=callback)
    asyncio.run(client._on_notification(
        None, make_wire(0, const.CMD_STATUS_RESPONSE,
                        [0x05, 0x00, 0x32, 0x28] + [0] * 6)))
    assert seen == []


def test_probe_reply_sets_the_probe_event_and_not_the_version_callback():
    versions, version_callback = collector()
    client = new_client(version_callback=version_callback)
    client._probe_reply_event = asyncio.Event()
    asyncio.run(client._on_notification(
        None, make_wire(5, const.CMD_STATUS_QUERY_RESPONSE, [0] * 10)))
    assert client._probe_reply_event.is_set()
    assert versions == []


def test_short_frames_are_ignored():
    client = new_client()
    asyncio.run(client._on_notification(None, bytearray([0] * 8)))


def test_notifications_are_ignored_before_a_session_key_exists():
    """Nothing can be decrypted before pairing completes."""
    client = cm.CyncMeshClient(hass=None, mesh_name=MESH_MAC, mesh_password="pw",
                               mesh_macs=[MESH_MAC])
    asyncio.run(client._on_notification(
        None, make_wire(1, const.CMD_STATUS_RESPONSE, [0x05, 0x01, 0x32, 0x28] + [0] * 6)))


# --------------------------------------------------------------------------
# Key derivation invariants
# --------------------------------------------------------------------------

def test_key_derivation_pads_and_truncates_to_16_bytes():
    """Both zips in the derivation are strict=True, so a padding regression
    raises here instead of silently deriving a wrong session key."""
    short = cm._generate_sk("ab", "cd", list(range(8)), list(range(8)))
    long = cm._generate_sk("a" * 40, "b" * 40, list(range(8)), list(range(8)))
    assert len(short) == 16
    assert len(long) == 16
