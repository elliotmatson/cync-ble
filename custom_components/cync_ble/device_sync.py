"""Pure diff/merge logic for re-syncing the stored device list from the cloud.

This module deliberately imports nothing from Home Assistant and does no
network I/O. Everything here is a plain function over plain dicts — the same
shape cync_cloud.get_devices() returns and the same shape the config entry
stores — so the interesting logic (what counts as the same device, what may
be overwritten, what must never be) is testable on its own, without standing
up a config flow or a cloud session.

Keep it that way: if something here needs `hass`, it belongs in config_flow.
"""
from __future__ import annotations

from typing import Any, Iterable

# Fields refreshed from the cloud by apply_diff. Everything else in a stored
# entry is preserved as-is.
#
# device_id is conspicuously NOT here, and must never be added — see
# apply_diff for why overwriting it would be actively dangerous.
#
# Nor are `mac` and `mesh_name`: those are the identity this whole module
# matches on, so a "change" to either is by definition a different device
# (an add plus a remove), not a field update to an existing one.
REFRESHABLE_FIELDS: tuple[str, ...] = (
    "name",
    "access_key",
    "mesh_display_name",
    "device_type",
    "supports_rgb",
    "supports_temperature",
    "is_plug",
    "is_fan",
)

# How many device names to name explicitly in a summary line before
# collapsing the rest into a count.
SUMMARY_LIMIT = 5


def normalize_mac(mac: Any) -> str:
    """Normalise a MAC to bare uppercase hex, or "" if there isn't one.

    Accepts the colon form the cloud client emits ("AA:BB:CC:DD:EE:FF"), the
    bare form the Cync API sometimes returns ("AABBCCDDEEFF"), and dashes.
    Anything that isn't a string, or is blank, becomes "" — i.e. "no MAC",
    which device_key() then handles by falling back.
    """
    if not isinstance(mac, str):
        return ""
    return mac.strip().upper().replace(":", "").replace("-", "")


def device_key(device: dict[str, Any]) -> str:
    """Stable identity for a device across cloud re-fetches.

    Matches on the normalised MAC when there is one, and only falls back to
    mesh_name/device_id when there isn't.

    The ordering matters and is not arbitrary. The mesh device_id is a
    reassignable 1-255 short address that the Cync app hands out — the Telink
    protocol spec (AN-BLE-15120202-E3 §2) has the app allocate it via the
    Device_Addr command and auto-increment for the next light, so it is
    explicitly re-allocatable, and adding or re-pairing bulbs can renumber
    existing ones. Keying on it primarily would make a renumbered bulb look
    like a delete plus an add: the entity built from the old key would be
    orphaned and a duplicate created alongside it, from a device that never
    physically moved.

    The MAC is stable for the life of the hardware, and is also what entity
    unique_ids are built from (see light.py / switch.py / fan.py), so keying
    on it is what keeps a re-sync from disturbing existing entities.
    """
    mac = normalize_mac(device.get("mac"))
    if mac:
        return f"mac:{mac}"
    # No MAC: mesh_name + device_id is the only identity left. It's weaker
    # for exactly the renumbering reason above, but a device with no MAC
    # can't be matched any other way, and treating every such device as
    # brand new on every sync would be worse.
    mesh = normalize_mac(device.get("mesh_name")) or str(device.get("mesh_name", ""))
    return f"mesh:{mesh}/{device.get('device_id')}"


def _index(devices: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index devices by key. On a duplicate key, the first entry wins so a
    malformed response can't silently replace a good entry with a worse one.
    """
    out: dict[str, dict[str, Any]] = {}
    for device in devices:
        key = device_key(device)
        out.setdefault(key, device)
    return out


def diff_devices(
    stored: list[dict[str, Any]], fetched: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compare the stored device list against a freshly fetched one.

    Returns:
        added     — devices present only in `fetched`
        removed   — devices present only in `stored`
        changed   — [{"key", "device", "fetched", "changes": {field: (old, new)}}]
                    for devices in both whose refreshable fields differ
        unchanged — devices in both with no refreshable differences

    Only REFRESHABLE_FIELDS are compared. A difference in any other field is
    not reported as a change, because apply_diff would not act on it anyway —
    reporting it would promise the user an update that never happens.
    """
    stored_index = _index(stored)
    fetched_index = _index(fetched)

    added = [d for k, d in fetched_index.items() if k not in stored_index]
    removed = [d for k, d in stored_index.items() if k not in fetched_index]

    changed: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    for key, stored_device in stored_index.items():
        fetched_device = fetched_index.get(key)
        if fetched_device is None:
            continue
        changes: dict[str, tuple[Any, Any]] = {}
        for field in REFRESHABLE_FIELDS:
            if field not in fetched_device:
                # The cloud didn't report this field at all. Absence is not
                # the same as "now empty", so leave the stored value alone
                # rather than reporting a change to None.
                continue
            old = stored_device.get(field)
            new = fetched_device[field]
            if old != new:
                changes[field] = (old, new)
        if changes:
            changed.append({
                "key": key,
                "device": stored_device,
                "fetched": fetched_device,
                "changes": changes,
            })
        else:
            unchanged.append(stored_device)

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
    }


def has_changes(diff: dict[str, Any], remove_missing: bool = False) -> bool:
    """Whether applying this diff would alter anything.

    `removed` only counts when remove_missing is on: with it off, devices
    missing from the cloud are kept, so their absence changes nothing and
    shouldn't make the flow offer an update that would be a no-op.
    """
    if diff["added"] or diff["changed"]:
        return True
    return bool(remove_missing and diff["removed"])


def apply_diff(
    stored: list[dict[str, Any]],
    fetched: list[dict[str, Any]],
    remove_missing: bool = False,
) -> list[dict[str, Any]]:
    """Produce the new device list by MERGING fetched data into stored.

    This is a merge, not a replacement, and that distinction is the whole
    point of the function:

      * Only REFRESHABLE_FIELDS are taken from the cloud. Any other field
        already on a stored entry is preserved untouched, so an entry written
        by an older version of the integration doesn't lose data that the
        current parser happens not to emit.

      * device_id is NEVER overwritten, even though the cloud reports one.
        It is the mesh short address every command is addressed to. If the
        Cync app renumbered a bulb, silently adopting the new id here would
        repoint that entity's commands at whichever *other* bulb now holds
        the old address — the user would toggle a light and a different light
        would respond, with nothing in the logs to explain it. Devices are
        matched by MAC (see device_key) precisely so a renumber is visible as
        a preserved device rather than being followed blindly.

      * remove_missing defaults to False. Removing a device deletes its
        entities and breaks every automation, script and dashboard card that
        references them, and a device can drop out of the cloud list for
        reasons that have nothing to do with the user removing it — a partial
        API response, or a failed per-mesh properties fetch. Losing entities
        must be something the user opted into.

    Newly added devices are appended as-is. Ordering is stable: existing
    entries keep their relative order, additions go on the end.
    """
    fetched_index = _index(fetched)
    stored_index = _index(stored)

    result: list[dict[str, Any]] = []
    for device in stored:
        key = device_key(device)
        fetched_device = fetched_index.get(key)

        if fetched_device is None:
            if remove_missing:
                continue
            result.append(dict(device))
            continue

        merged = dict(device)
        for field in REFRESHABLE_FIELDS:
            if field in fetched_device:
                merged[field] = fetched_device[field]
        result.append(merged)

    for key, device in fetched_index.items():
        if key not in stored_index:
            result.append(dict(device))

    return result


def _device_label(device: dict[str, Any]) -> str:
    """Human-readable label for one device."""
    name = device.get("name") or f"Cync {device.get('device_id')}"
    mac = device.get("mac")
    return f"{name} ({mac})" if mac else str(name)


def _join(labels: list[str], limit: int = SUMMARY_LIMIT) -> str:
    """Join labels, collapsing the tail past `limit` into a count.

    The result goes into a config-flow form description, which has no
    scrolling to speak of — a user with 40 new bulbs needs to see that it's
    40, not 40 lines of text.
    """
    if len(labels) <= limit:
        return ", ".join(labels)
    shown = ", ".join(labels[:limit])
    return f"{shown}, and {len(labels) - limit} more"


def summarize(diff: dict[str, Any]) -> dict[str, str]:
    """Render a diff into the placeholders the confirm step's text expects.

    Returns exactly these keys, always populated (never absent, never None),
    because a missing placeholder makes Home Assistant fail to render the
    form rather than degrade gracefully:

        added_count, added, removed_count, removed,
        changed_count, changed, unchanged_count
    """
    added = diff["added"]
    removed = diff["removed"]
    changed = diff["changed"]

    added_labels = [_device_label(d) for d in added]
    removed_labels = [_device_label(d) for d in removed]

    changed_labels: list[str] = []
    for entry in changed:
        fields = ", ".join(
            f"{field}: {old!r} -> {new!r}"
            for field, (old, new) in sorted(entry["changes"].items())
        )
        changed_labels.append(f"{_device_label(entry['device'])} [{fields}]")

    return {
        "added_count": str(len(added)),
        "added": _join(added_labels) or "none",
        "removed_count": str(len(removed)),
        "removed": _join(removed_labels) or "none",
        "changed_count": str(len(changed)),
        "changed": _join(changed_labels) or "none",
        "unchanged_count": str(len(diff["unchanged"])),
    }
