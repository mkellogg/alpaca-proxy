"""YAML configuration loading and validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

VALID_DEVICE_TYPES = ("safetymonitor", "observingconditions")

_DEFAULTS: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 11111,
    "poll_interval_seconds": 10.0,
    "failure_threshold": 5,
    "request_timeout_seconds": 10.0,
    "unsafe_on_weather_failure": True,
    "client_id": 1,
    "log_file": None,
    "log_level": "INFO",
}

_DEVICE_DEFAULTS: dict[str, dict[str, Any]] = {
    "safety": {
        "device_type": "safetymonitor",
        "local_device_number": 0,
        "name": "Safety Monitor",
    },
    "weather": {
        "device_type": "observingconditions",
        "local_device_number": 0,
        "name": "Observing Conditions",
    },
}


@dataclass(frozen=True)
class DeviceConfig:
    device_type: str
    upstream_device_number: int
    local_device_number: int
    name: str


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    poll_interval_seconds: float
    failure_threshold: int
    request_timeout_seconds: float
    unsafe_on_weather_failure: bool
    upstream_base_url: str
    client_id: int
    log_file: str | None
    log_level: str
    safety: DeviceConfig
    weather: DeviceConfig


def load_config(path: str | Path) -> Config:
    """Load and validate the YAML config at *path*.

    Raises ValueError with a human-readable message on any invalid value.
    """
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"config file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"config file {path} is not valid YAML: {exc}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"config file {path} must contain a YAML mapping")

    base_url = raw.get("upstream_base_url")
    if not base_url or not isinstance(base_url, str):
        raise ValueError("upstream_base_url is required and must be a string")
    base_url = base_url.strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError(
            f"upstream_base_url must start with http:// or https://, got {base_url!r}"
        )

    devices = raw.get("devices")
    if not isinstance(devices, dict):
        raise ValueError("devices section is required and must be a mapping")

    port = _as_int(raw, "port")
    if not 1 <= port <= 65535:
        raise ValueError(f"port must be between 1 and 65535, got {port}")

    poll_interval = _as_float(raw, "poll_interval_seconds")
    if poll_interval <= 0:
        raise ValueError(f"poll_interval_seconds must be > 0, got {poll_interval}")

    timeout = _as_float(raw, "request_timeout_seconds")
    if timeout <= 0:
        raise ValueError(f"request_timeout_seconds must be > 0, got {timeout}")

    threshold = _as_int(raw, "failure_threshold")
    if threshold < 1:
        raise ValueError(f"failure_threshold must be >= 1, got {threshold}")

    log_file = raw.get("log_file", _DEFAULTS["log_file"])
    if log_file is not None and not isinstance(log_file, str):
        raise ValueError("log_file must be a string or null")

    log_level = raw.get("log_level", _DEFAULTS["log_level"])
    if not isinstance(log_level, str):
        raise ValueError("log_level must be a string")

    return Config(
        host=str(raw.get("host", _DEFAULTS["host"])),
        port=port,
        poll_interval_seconds=poll_interval,
        failure_threshold=threshold,
        request_timeout_seconds=timeout,
        unsafe_on_weather_failure=bool(
            raw.get("unsafe_on_weather_failure", _DEFAULTS["unsafe_on_weather_failure"])
        ),
        upstream_base_url=base_url,
        client_id=_as_int(raw, "client_id"),
        log_file=log_file,
        log_level=log_level.upper(),
        safety=_device(devices, "safety"),
        weather=_device(devices, "weather"),
    )


def _device(devices: dict[str, Any], slot: str) -> DeviceConfig:
    section = devices.get(slot)
    if not isinstance(section, dict):
        raise ValueError(f"devices.{slot} section is required and must be a mapping")

    defaults = _DEVICE_DEFAULTS[slot]
    device_type = section.get("device_type", defaults["device_type"])
    if device_type not in VALID_DEVICE_TYPES:
        raise ValueError(
            f"devices.{slot}.device_type must be one of "
            f"{', '.join(VALID_DEVICE_TYPES)}, got {device_type!r}"
        )

    if "upstream_device_number" not in section:
        raise ValueError(f"devices.{slot}.upstream_device_number is required")

    upstream_number = _device_int(section, slot, "upstream_device_number")
    if upstream_number < 0:
        raise ValueError(
            f"devices.{slot}.upstream_device_number must be >= 0, got {upstream_number}"
        )

    section = {**defaults, **section}
    local_number = _device_int(section, slot, "local_device_number")
    if local_number < 0:
        raise ValueError(
            f"devices.{slot}.local_device_number must be >= 0, got {local_number}"
        )

    return DeviceConfig(
        device_type=device_type,
        upstream_device_number=upstream_number,
        local_device_number=local_number,
        name=str(section["name"]),
    )


def _as_int(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key, _DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer, got {value!r}")
    return value


def _as_float(raw: dict[str, Any], key: str) -> float:
    value = raw.get(key, _DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number, got {value!r}")
    return float(value)


def _device_int(section: dict[str, Any], slot: str, key: str) -> int:
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"devices.{slot}.{key} must be an integer, got {value!r}")
    return value
