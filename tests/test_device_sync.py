"""Tests for device_sync — the pure diff/merge logic behind the re-sync flow.

This module has no HA or network imports, so these run against it directly.
"""
from __future__ import annotations

import pytest
from conftest import load

ds = load("device_sync")


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------

def test_key_is_mac_based_when_a_mac_exists(make_device):
    assert ds.device_key(make_device()) == "mac:AABBCCDDEE01"


@pytest.mark.parametrize(
    "mac",
    ["AABBCCDDEE01", "aa-bb-cc-dd-ee-01", "aa:bb:cc:dd:ee:01", "  AA:BB:CC:DD:EE:01  "],
    ids=["bare", "dashes", "lowercase-colons", "whitespace-padded"],
)
def test_mac_normalisation_matches_equivalent_forms(make_device, mac):
    """All spellings of one MAC must resolve to the same device."""
    diff = ds.diff_devices([make_device()], [make_device(mac=mac, name="Renamed")])
    assert (len(diff["added"]), len(diff["removed"]), len(diff["changed"])) == (0, 0, 1)


def test_key_falls_back_to_mesh_and_device_id_without_a_mac(make_device):
    assert ds.device_key(make_device(mac="")) == "mesh:112233445566/5"


def test_non_string_mac_is_treated_as_absent(make_device):
    assert ds.device_key(make_device(mac=None)) == "mesh:112233445566/5"


def test_devices_without_macs_still_match_each_other(make_device):
    diff = ds.diff_devices([make_device(mac="")], [make_device(mac="", name="Renamed")])
    assert (len(diff["added"]), len(diff["changed"])) == (0, 1)


def test_no_mac_plus_different_device_id_is_add_and_remove(make_device):
    """The weakness of the fallback, asserted so it isn't mistaken for a bug."""
    diff = ds.diff_devices([make_device(mac="")], [make_device(mac="", device_id=7)])
    assert (len(diff["added"]), len(diff["removed"])) == (1, 1)


# --------------------------------------------------------------------------
# Diffing
# --------------------------------------------------------------------------

def test_new_device_is_detected(make_device):
    old = make_device()
    new = make_device(name="New", device_id=9, mac="AA:BB:CC:DD:EE:02")
    diff = ds.diff_devices([old], [old, new])
    assert [d["name"] for d in diff["added"]] == ["New"]
    assert diff["removed"] == []
    assert len(diff["unchanged"]) == 1
    assert ds.has_changes(diff) is True


def test_rename_is_a_change_not_an_add_and_remove(make_device):
    diff = ds.diff_devices([make_device(name="Old")], [make_device(name="New")])
    assert (len(diff["added"]), len(diff["removed"]), len(diff["changed"])) == (0, 0, 1)
    assert diff["changed"][0]["changes"] == {"name": ("Old", "New")}


def test_field_absent_from_the_response_is_not_reported_as_a_change(make_device):
    """Absence != "now empty". A partial response must not look like a wave
    of changes to None."""
    partial = {"mac": "AA:BB:CC:DD:EE:01", "device_id": 5,
               "mesh_name": "11:22:33:44:55:66"}
    assert ds.diff_devices([make_device()], [partial])["changed"] == []


def test_identical_lists_report_no_changes(make_device):
    devices = [make_device(), make_device(name="Two", device_id=9,
                                          mac="AA:BB:CC:DD:EE:02")]
    diff = ds.diff_devices(devices, devices)
    assert (diff["added"], diff["removed"], diff["changed"]) == ([], [], [])
    assert len(diff["unchanged"]) == 2
    assert ds.has_changes(diff) is False
    assert ds.has_changes(diff, remove_missing=True) is False


def test_duplicate_keys_in_a_response_resolve_first_wins(make_device):
    """A malformed response must not replace a good entry with a worse one."""
    duplicates = [make_device(name="First"), make_device(name="Second")]
    merged = ds.apply_diff([make_device(name="Orig")], duplicates)
    assert merged[0]["name"] == "First"


# --------------------------------------------------------------------------
# Removal is opt-in
# --------------------------------------------------------------------------

def test_removal_defaults_to_off(make_device):
    keep = make_device()
    gone = make_device(name="Gone", device_id=9, mac="AA:BB:CC:DD:EE:02")
    assert [d["name"] for d in ds.apply_diff([keep, gone], [keep])] == ["Bulb", "Gone"]


def test_removal_happens_only_when_opted_in(make_device):
    keep = make_device()
    gone = make_device(name="Gone", device_id=9, mac="AA:BB:CC:DD:EE:02")
    result = ds.apply_diff([keep, gone], [keep], remove_missing=True)
    assert [d["name"] for d in result] == ["Bulb"]


def test_missing_only_diff_is_not_a_change_unless_removal_is_on(make_device):
    keep = make_device()
    gone = make_device(name="Gone", device_id=9, mac="AA:BB:CC:DD:EE:02")
    diff = ds.diff_devices([keep, gone], [keep])
    assert ds.has_changes(diff) is False
    assert ds.has_changes(diff, remove_missing=True) is True


def test_empty_fetched_list_changes_nothing_without_opt_in(make_device):
    assert len(ds.apply_diff([make_device()], [])) == 1


# --------------------------------------------------------------------------
# Merge safety
# --------------------------------------------------------------------------

def test_device_id_is_never_overwritten_on_renumber(make_device):
    """Adopting a new device_id would repoint commands at a different bulb."""
    stored = make_device(device_id=5, name="Lamp")
    cloud = make_device(device_id=42, name="Lamp Renamed")
    diff = ds.diff_devices([stored], [cloud])

    assert (len(diff["added"]), len(diff["removed"])) == (0, 0)
    assert "device_id" not in diff["changed"][0]["changes"]

    merged = ds.apply_diff([stored], [cloud])[0]
    assert merged["device_id"] == 5
    assert merged["name"] == "Lamp Renamed"


@pytest.mark.parametrize("field", ["device_id", "mac", "mesh_name"])
def test_identity_fields_are_not_refreshable(field):
    assert field not in ds.REFRESHABLE_FIELDS


def test_unknown_stored_fields_are_preserved(make_device):
    """An entry written by an older version must not lose data the current
    parser doesn't emit."""
    stored = make_device(legacy_field="keep me", nested={"a": 1})
    merged = ds.apply_diff([stored], [make_device(name="Renamed")])[0]
    assert merged["legacy_field"] == "keep me"
    assert merged["nested"] == {"a": 1}
    assert merged["name"] == "Renamed"


def test_apply_diff_does_not_mutate_the_stored_entries(make_device):
    stored = make_device(name="Original")
    ds.apply_diff([stored], [make_device(name="Renamed")])
    assert stored["name"] == "Original"


def test_apply_diff_is_idempotent(make_device):
    devices = [make_device(), make_device(name="Two", device_id=9,
                                          mac="AA:BB:CC:DD:EE:02")]
    once = ds.apply_diff(devices, devices)
    assert once == devices
    assert ds.apply_diff(once, devices) == once


def test_resync_after_apply_reports_nothing_new(make_device):
    fetched = [make_device(name="X")]
    applied = ds.apply_diff([make_device()], fetched)
    assert ds.has_changes(ds.diff_devices(applied, fetched)) is False


def test_ordering_is_stable_with_additions_appended(make_device):
    stored = [make_device(name="A", mac="AA:BB:CC:DD:EE:01"),
              make_device(name="B", mac="AA:BB:CC:DD:EE:02")]
    fetched = [make_device(name="B", mac="AA:BB:CC:DD:EE:02"),
               make_device(name="C", mac="AA:BB:CC:DD:EE:03"),
               make_device(name="A", mac="AA:BB:CC:DD:EE:01")]
    assert [d["name"] for d in ds.apply_diff(stored, fetched)] == ["A", "B", "C"]


# --------------------------------------------------------------------------
# summarize
# --------------------------------------------------------------------------

REQUIRED_PLACEHOLDERS = {
    "added_count", "added", "removed_count", "removed",
    "changed_count", "changed", "unchanged_count",
}


def test_summarize_returns_exactly_the_documented_placeholders(make_device):
    summary = ds.summarize(ds.diff_devices([make_device()], [make_device()]))
    assert set(summary) == REQUIRED_PLACEHOLDERS


def test_summarize_values_are_always_non_empty_strings(make_device):
    """A missing or None placeholder makes HA fail to render the form."""
    summary = ds.summarize(ds.diff_devices([make_device()], [make_device()]))
    assert all(isinstance(v, str) and v for v in summary.values())


def test_summarize_renders_empty_sections_as_none_not_blank(make_device):
    summary = ds.summarize(ds.diff_devices([make_device()], [make_device()]))
    assert (summary["added"], summary["removed"], summary["changed"]) == \
        ("none", "none", "none")


def test_summarize_includes_the_field_delta(make_device):
    summary = ds.summarize(
        ds.diff_devices([make_device(name="Old")], [make_device(name="New")])
    )
    assert "'Old' -> 'New'" in summary["changed"]


def test_summarize_truncates_long_lists_but_keeps_an_exact_count(make_device):
    fetched = [make_device(mac=f"AA:BB:CC:DD:EE:{i:02X}") for i in range(12)]
    summary = ds.summarize(ds.diff_devices([], fetched))
    assert "and 7 more" in summary["added"]
    assert summary["added_count"] == "12"


def test_empty_inputs_do_not_crash():
    assert ds.has_changes(ds.diff_devices([], [])) is False
