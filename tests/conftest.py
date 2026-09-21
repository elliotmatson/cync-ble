"""Test harness for the Cync BLE integration.

Home Assistant is not a test dependency here. The modules under test import
HA only for types, base classes and a handful of helpers, so the whole HA
surface they touch is stubbed into sys.modules before they are imported.

Why not pytest-homeassistant-custom-component: it pins a specific HA version
and drags in the full core test fixtures. That buys fidelity for entity
plumbing, but the logic actually worth protecting here — packet decoding,
device diffing, availability reasoning — is independent of HA, and pinning
would mean CI breaking on HA releases rather than on this repo's changes.
The tradeoff is explicit: these tests verify *our* logic, not HA's contract
with us. hassfest and the HACS action in CI cover the manifest/structure
side that stubs cannot.

The integration is imported under the synthetic package name `cync_pkg`
whose __path__ points at the real directory. That makes relative imports
(`from .const import ...`) resolve without executing the package's
__init__.py, which would pull in the rest of HA.
"""
from __future__ import annotations

import datetime
import importlib
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPONENT_DIR = REPO_ROOT / "custom_components" / "cync_ble"


def _mod(name: str, **attrs: object) -> types.ModuleType:
    """Register a stub module in sys.modules."""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _StubEntity:
    """Stands in for the HA entity base classes."""

    def async_write_ha_state(self) -> None:
        pass

    def async_on_remove(self, func: object) -> None:
        pass


class _StubCoordinatorBase:
    """Minimal DataUpdateCoordinator: just the listener plumbing we use."""

    def __init__(self, hass, logger, name=None, update_interval=None) -> None:
        self.hass = hass
        self._listeners: list = []

    def async_add_listener(self, callback):
        self._listeners.append(callback)
        return lambda: None

    def async_update_listeners(self) -> None:
        for callback in self._listeners:
            callback()


class _StubConfigFlow:
    """HA's ConfigFlow accepts a `domain=` class keyword at subclass time."""

    def __init_subclass__(cls, domain=None, **kwargs) -> None:
        super().__init_subclass__(**kwargs)


class _StubMarker:
    """voluptuous Required/Optional marker, enough to inspect schema keys."""

    def __init__(self, key, default=None) -> None:
        self.key = key
        self.default = default

    def __hash__(self) -> int:
        return hash(self.key)

    def __eq__(self, other) -> bool:
        return self.key == getattr(other, "key", other)

    def __str__(self) -> str:
        return str(self.key)


def _install_stubs() -> None:
    """Install every third-party import site the component touches.

    Done at conftest import time, before pytest collects test modules, since
    those import the component at module level.
    """
    _mod("bleak_retry_connector", establish_connection=None)

    ha = _mod("homeassistant")
    ha.__path__ = []  # type: ignore[attr-defined]
    components = _mod("homeassistant.components")
    components.__path__ = []  # type: ignore[attr-defined]
    helpers = _mod("homeassistant.helpers")
    helpers.__path__ = []  # type: ignore[attr-defined]
    util = _mod("homeassistant.util")
    util.__path__ = []  # type: ignore[attr-defined]

    _mod(
        "homeassistant.components.bluetooth",
        async_ble_device_from_address=lambda *a, **k: None,
        async_register_callback=lambda *a, **k: (lambda: None),
        BluetoothCallbackMatcher=lambda **k: None,
        BluetoothChange=types.SimpleNamespace(ADVERTISEMENT="advertisement"),
        BluetoothServiceInfoBleak=object,
    )
    _mod(
        "homeassistant.core",
        HomeAssistant=object,
        callback=lambda func: func,
        ServiceCall=object,
        ServiceResponse=dict,
        SupportsResponse=types.SimpleNamespace(ONLY="only"),
    )
    _mod(
        "homeassistant.helpers.issue_registry",
        async_create_issue=lambda *a, **k: None,
        async_delete_issue=lambda *a, **k: None,
        IssueSeverity=types.SimpleNamespace(WARNING="warning"),
    )
    _mod(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=_StubCoordinatorBase,
    )
    _mod(
        "homeassistant.util.dt",
        utcnow=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    _mod(
        "homeassistant.helpers.device_registry",
        DeviceEntryType=types.SimpleNamespace(SERVICE="service"),
    )
    _mod(
        "homeassistant.helpers.entity",
        DeviceInfo=dict,
        EntityCategory=types.SimpleNamespace(DIAGNOSTIC="diagnostic"),
    )
    _mod("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
    _mod("homeassistant.config_entries", ConfigEntry=object,
         ConfigFlow=_StubConfigFlow)
    _mod("homeassistant.data_entry_flow", FlowResult=dict)
    _mod("homeassistant.exceptions", ConfigEntryNotReady=Exception)
    _mod("homeassistant.helpers.typing", ConfigType=dict)
    _mod(
        "homeassistant.components.sensor",
        SensorEntity=_StubEntity,
        SensorDeviceClass=types.SimpleNamespace(TIMESTAMP="timestamp"),
        SensorStateClass=types.SimpleNamespace(MEASUREMENT="measurement"),
    )
    _mod(
        "homeassistant.components.binary_sensor",
        BinarySensorEntity=_StubEntity,
        BinarySensorDeviceClass=types.SimpleNamespace(CONNECTIVITY="connectivity"),
    )

    _mod("aiohttp", ClientSession=object, ClientError=Exception,
         ClientTimeout=lambda **k: None)
    _mod(
        "voluptuous",
        Schema=lambda d, **k: d,
        Required=lambda k, **kw: _StubMarker(k),
        Optional=lambda k, **kw: _StubMarker(k, kw.get("default")),
        All=lambda *a, **k: None,
        Coerce=lambda *a, **k: None,
        Range=lambda **k: None,
    )

    package = types.ModuleType("cync_pkg")
    package.__path__ = [str(COMPONENT_DIR)]  # type: ignore[attr-defined]
    sys.modules["cync_pkg"] = package


_install_stubs()


def load(module: str) -> types.ModuleType:
    """Import a component module by its bare name, e.g. load("cync_mesh")."""
    return importlib.import_module(f"cync_pkg.{module}")


@pytest.fixture(scope="session")
def const():
    return load("const")


@pytest.fixture(scope="session")
def cync_mesh():
    return load("cync_mesh")


@pytest.fixture(scope="session")
def coordinator_mod():
    return load("coordinator")


@pytest.fixture(scope="session")
def device_sync():
    return load("device_sync")


@pytest.fixture(scope="session")
def config_flow():
    return load("config_flow")


@pytest.fixture
def make_device():
    """Build a cloud-shaped device dict, overridable per field."""

    def _make(**overrides):
        device = {
            "name": "Bulb",
            "device_id": 5,
            "mac": "AA:BB:CC:DD:EE:01",
            "mesh_name": "11:22:33:44:55:66",
            "access_key": "key1",
            "mesh_display_name": "Home",
            "device_type": 6,
            "supports_rgb": True,
            "supports_temperature": True,
            "is_plug": False,
            "is_fan": False,
        }
        device.update(overrides)
        return device

    return _make
