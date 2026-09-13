from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from alpaca_proxy.config import load_config

MINIMAL = {
    "upstream_base_url": "https://alpaca-api.example.com",
    "devices": {
        "safety": {"upstream_device_number": 8},
        "weather": {"upstream_device_number": 1},
    },
}


def write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def load(tmp_path: Path, **overrides: object):
    raw = {**MINIMAL, **overrides}
    return load_config(write(tmp_path, raw))


def test_defaults(tmp_path: Path) -> None:
    cfg = load(tmp_path)
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 11111
    assert cfg.poll_interval_seconds == 10.0
    assert cfg.failure_threshold == 5
    assert cfg.request_timeout_seconds == 10.0
    assert cfg.unsafe_on_weather_failure is True
    assert cfg.client_id == 1
    assert cfg.log_level == "INFO"
    assert cfg.log_file is None
    assert cfg.upstream_base_url == "https://alpaca-api.example.com"


def test_device_defaults(tmp_path: Path) -> None:
    cfg = load(tmp_path)
    assert cfg.safety.device_type == "safetymonitor"
    assert cfg.safety.upstream_device_number == 8
    assert cfg.safety.local_device_number == 0
    assert cfg.weather.device_type == "observingconditions"
    assert cfg.weather.upstream_device_number == 1
    assert cfg.weather.local_device_number == 0


def test_full_config(tmp_path: Path) -> None:
    cfg = load(
        tmp_path,
        host="0.0.0.0",
        port=12345,
        poll_interval_seconds=2.5,
        failure_threshold=3,
        request_timeout_seconds=4,
        unsafe_on_weather_failure=False,
        client_id=77,
        log_level="debug",
        log_file="C:/logs/proxy.log",
        devices={
            "safety": {
                "device_type": "safetymonitor",
                "upstream_device_number": 8,
                "local_device_number": 2,
                "name": "Test Safety Monitor",
            },
            "weather": {
                "device_type": "observingconditions",
                "upstream_device_number": 1,
                "local_device_number": 3,
                "name": "Test Weather Station",
            },
        },
    )
    assert (cfg.host, cfg.port) == ("0.0.0.0", 12345)
    assert cfg.poll_interval_seconds == 2.5
    assert cfg.failure_threshold == 3
    assert cfg.request_timeout_seconds == 4.0
    assert cfg.unsafe_on_weather_failure is False
    assert cfg.client_id == 77
    assert cfg.log_level == "DEBUG"
    assert cfg.log_file == "C:/logs/proxy.log"
    assert cfg.safety.name == "Test Safety Monitor"
    assert cfg.safety.local_device_number == 2
    assert cfg.weather.name == "Test Weather Station"
    assert cfg.weather.local_device_number == 3


def test_config_is_frozen(tmp_path: Path) -> None:
    cfg = load(tmp_path)
    with pytest.raises(Exception):
        cfg.port = 1  # type: ignore[misc]


def test_trailing_slash_stripped(tmp_path: Path) -> None:
    cfg = load(tmp_path, upstream_base_url="https://example.test/")
    assert cfg.upstream_base_url == "https://example.test"


def test_example_config_loads() -> None:
    example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    cfg = load_config(example)
    assert cfg.upstream_base_url == "https://alpaca-api.example.com"
    assert cfg.safety.upstream_device_number == 0
    assert cfg.safety.name == "Safety Monitor"
    assert cfg.weather.upstream_device_number == 1
    assert cfg.weather.name == "Weather Station"
    assert cfg.safety.local_device_number == 0
    assert cfg.weather.local_device_number == 0


# -- validation errors -------------------------------------------------------


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_not_a_mapping(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_config(path)


def test_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("a: [1,\n", encoding="utf-8")
    with pytest.raises(ValueError, match="valid YAML"):
        load_config(path)


def test_missing_base_url(tmp_path: Path) -> None:
    raw = {k: v for k, v in MINIMAL.items() if k != "upstream_base_url"}
    with pytest.raises(ValueError, match="upstream_base_url is required"):
        load_config(write(tmp_path, raw))


@pytest.mark.parametrize("url", ["ftp://example.test", "example.test", "ws://x"])
def test_bad_base_url_scheme(tmp_path: Path, url: str) -> None:
    with pytest.raises(ValueError, match="http"):
        load(tmp_path, upstream_base_url=url)


def test_missing_devices(tmp_path: Path) -> None:
    raw = {k: v for k, v in MINIMAL.items() if k != "devices"}
    with pytest.raises(ValueError, match="devices section is required"):
        load_config(write(tmp_path, raw))


def test_missing_device_section(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="devices.weather section is required"):
        load(tmp_path, devices={"safety": {"upstream_device_number": 8}})


def test_missing_upstream_device_number(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="devices.safety.upstream_device_number"):
        load(
            tmp_path,
            devices={"safety": {}, "weather": {"upstream_device_number": 1}},
        )


def test_unknown_device_type(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="device_type"):
        load(
            tmp_path,
            devices={
                "safety": {"device_type": "telescope", "upstream_device_number": 8},
                "weather": {"upstream_device_number": 1},
            },
        )


@pytest.mark.parametrize("port", [0, -1, 65536, 99999])
def test_port_out_of_range(tmp_path: Path, port: int) -> None:
    with pytest.raises(ValueError, match="port must be between"):
        load(tmp_path, port=port)


@pytest.mark.parametrize("interval", [0, -1, -0.5])
def test_non_positive_poll_interval(tmp_path: Path, interval: float) -> None:
    with pytest.raises(ValueError, match="poll_interval_seconds must be > 0"):
        load(tmp_path, poll_interval_seconds=interval)


@pytest.mark.parametrize("timeout", [0, -3])
def test_non_positive_timeout(tmp_path: Path, timeout: float) -> None:
    with pytest.raises(ValueError, match="request_timeout_seconds must be > 0"):
        load(tmp_path, request_timeout_seconds=timeout)


@pytest.mark.parametrize("threshold", [0, -2])
def test_failure_threshold_too_low(tmp_path: Path, threshold: int) -> None:
    with pytest.raises(ValueError, match="failure_threshold must be >= 1"):
        load(tmp_path, failure_threshold=threshold)


def test_non_numeric_port(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="port must be an integer"):
        load(tmp_path, port="eleven")


def test_negative_device_number(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="upstream_device_number must be >= 0"):
        load(
            tmp_path,
            devices={
                "safety": {"upstream_device_number": -1},
                "weather": {"upstream_device_number": 1},
            },
        )
