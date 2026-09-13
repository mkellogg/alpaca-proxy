"""Protocol-level tests for the Alpaca surface.

These use a duck-typed fake poller, so nothing here touches the network or
depends on the real poller's caching/failure behaviour.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from alpaca_proxy import server
from alpaca_proxy.config import Config, DeviceConfig
from alpaca_proxy.poller import ValueNotSet
from alpaca_proxy.server import create_app
from alpaca_proxy.upstream import UpstreamNotImplemented

BASE_URL = "https://alpaca-api.example.com"


class FakeDeviceState:
    def __init__(self) -> None:
        self.consecutive_failures = 0
        self.last_success_utc: datetime | None = datetime(2026, 9, 12, 3, 0, tzinfo=timezone.utc)
        self.values: dict[str, Any] = {}
        self.not_implemented: set[str] = set()


class FakePoller:
    """Duck-typed to the poller contract used by server.py."""

    def __init__(self) -> None:
        self.safety = FakeDeviceState()
        self.weather = FakeDeviceState()
        self.safe = True
        self.values: dict[str, Any] = {"temperature": 12.5, "humidity": 44.0}
        self.not_implemented: set[str] = {"cloudcover", "skyquality", "starfwhm"}
        self.explode = False

    def is_safe(self) -> bool:
        if self.explode:
            raise RuntimeError("boom")
        return self.safe

    def weather_value(self, method: str) -> Any:
        if self.explode:
            raise RuntimeError("boom")
        if method in self.not_implemented:
            raise UpstreamNotImplemented(method)
        if method not in self.values:
            raise ValueNotSet(method)
        return self.values[method]

    def status(self) -> dict[str, Any]:
        return {"safe": self.safe, "safety_failures": 0, "weather_failures": 0}


def make_config() -> Config:
    return Config(
        host="127.0.0.1",
        port=11111,
        poll_interval_seconds=10.0,
        failure_threshold=5,
        request_timeout_seconds=10.0,
        unsafe_on_weather_failure=True,
        upstream_base_url=BASE_URL,
        client_id=1,
        log_file=None,
        log_level="INFO",
        safety=DeviceConfig("safetymonitor", 8, 0, "Test Safety Monitor"),
        weather=DeviceConfig("observingconditions", 1, 0, "Test Weather Station"),
    )


@pytest.fixture
def poller() -> FakePoller:
    return FakePoller()


@pytest.fixture
def client(poller: FakePoller) -> TestClient:
    return TestClient(create_app(make_config(), poller))


# --- management ----------------------------------------------------------


def test_apiversions(client: TestClient) -> None:
    body = client.get("/management/apiversions").json()
    assert body["Value"] == [1]
    assert body["ErrorNumber"] == 0


def test_description(client: TestClient) -> None:
    body = client.get("/management/v1/description").json()
    assert set(body["Value"]) == {
        "ServerName",
        "Manufacturer",
        "ManufacturerVersion",
        "Location",
    }


def test_configureddevices(client: TestClient) -> None:
    devices = client.get("/management/v1/configureddevices").json()["Value"]
    assert [d["DeviceType"] for d in devices] == ["SafetyMonitor", "ObservingConditions"]
    assert [d["DeviceNumber"] for d in devices] == [0, 0]
    assert len({d["UniqueID"] for d in devices}) == 2
    # deterministic across app instances
    again = TestClient(create_app(make_config(), FakePoller()))
    assert again.get("/management/v1/configureddevices").json()["Value"] == devices


def test_status_is_raw_poller_status(client: TestClient) -> None:
    assert client.get("/status").json() == {
        "safe": True,
        "safety_failures": 0,
        "weather_failures": 0,
    }


# --- envelope ------------------------------------------------------------


def test_issafe_reflects_poller(client: TestClient, poller: FakePoller) -> None:
    assert client.get("/api/v1/safetymonitor/0/issafe").json()["Value"] is True
    poller.safe = False
    body = client.get("/api/v1/safetymonitor/0/issafe").json()
    assert body["Value"] is False
    assert body["ErrorNumber"] == 0
    assert body["ErrorMessage"] == ""


def test_server_transaction_id_is_monotonic(client: TestClient) -> None:
    first = client.get("/api/v1/safetymonitor/0/issafe").json()["ServerTransactionID"]
    second = client.get("/api/v1/safetymonitor/0/issafe").json()["ServerTransactionID"]
    assert second > first


@pytest.mark.parametrize(
    "query,expected",
    [
        ("?ClientTransactionID=42", 42),
        ("?clienttransactionid=42", 42),
        ("", 0),
        ("?ClientTransactionID=", 0),
        ("?ClientTransactionID=banana", 0),
        ("?ClientTransactionID=-7", 0),
    ],
)
def test_client_transaction_id_echo(client: TestClient, query: str, expected: int) -> None:
    body = client.get(f"/api/v1/safetymonitor/0/issafe{query}").json()
    assert body["ClientTransactionID"] == expected


@pytest.mark.parametrize("name", ["ClientID", "clientid", "CLIENTID"])
def test_client_id_case_insensitive(client: TestClient, name: str) -> None:
    response = client.get(f"/api/v1/safetymonitor/0/issafe?{name}=99&{name.lower()}txid=1")
    assert response.status_code == 200
    assert response.json()["ErrorNumber"] == 0


def test_uppercase_device_type_and_method(client: TestClient) -> None:
    body = client.get("/api/v1/SafetyMonitor/0/IsSafe").json()
    assert body["ErrorNumber"] == 0
    assert body["Value"] is True


# --- errors --------------------------------------------------------------


def test_unknown_method_is_1024(client: TestClient) -> None:
    response = client.get("/api/v1/safetymonitor/0/nonsense")
    assert response.status_code == 200
    body = response.json()
    assert body["ErrorNumber"] == 1024
    assert "Value" not in body


def test_wrong_device_number_is_1025(client: TestClient) -> None:
    body = client.get("/api/v1/safetymonitor/8/issafe").json()
    assert body["ErrorNumber"] == 1025


def test_non_numeric_device_number_is_1025(client: TestClient) -> None:
    body = client.get("/api/v1/safetymonitor/abc/issafe").json()
    assert body["ErrorNumber"] == 1025


def test_unknown_device_type_is_1025(client: TestClient) -> None:
    body = client.get("/api/v1/telescope/0/connected").json()
    assert body["ErrorNumber"] == 1025


def test_poller_exception_is_200_with_envelope(client: TestClient, poller: FakePoller) -> None:
    poller.explode = True
    response = client.get("/api/v1/safetymonitor/0/issafe")
    assert response.status_code == 200
    body = response.json()
    assert body["ErrorNumber"] == 1280
    assert "internal proxy error" in body["ErrorMessage"]
    assert "Value" not in body


# --- observingconditions -------------------------------------------------


def test_weather_value(client: TestClient) -> None:
    assert client.get("/api/v1/observingconditions/0/temperature").json()["Value"] == 12.5


def test_cloudcover_passes_through_1024(client: TestClient) -> None:
    body = client.get("/api/v1/observingconditions/0/cloudcover").json()
    assert body["ErrorNumber"] == 1024
    assert body["ErrorMessage"] == "not implemented"


def test_value_not_set_is_1026(client: TestClient) -> None:
    body = client.get("/api/v1/observingconditions/0/pressure").json()
    assert body["ErrorNumber"] == 1026


def test_sensordescription(client: TestClient) -> None:
    body = client.get("/api/v1/observingconditions/0/sensordescription?SensorName=Temperature").json()
    assert body["ErrorNumber"] == 0
    assert isinstance(body["Value"], str) and body["Value"]
    lower = client.get("/api/v1/observingconditions/0/sensordescription?sensorname=humidity").json()
    assert lower["ErrorNumber"] == 0
    unknown = client.get("/api/v1/observingconditions/0/sensordescription?SensorName=Nope").json()
    assert unknown["ErrorNumber"] == 1025


def test_timesincelastupdate(client: TestClient, poller: FakePoller) -> None:
    poller.weather.last_success_utc = datetime.now(timezone.utc) - timedelta(seconds=30)
    body = client.get("/api/v1/observingconditions/0/timesincelastupdate").json()
    assert 29 <= body["Value"] <= 45

    poller.weather.last_success_utc = None
    body = client.get("/api/v1/observingconditions/0/timesincelastupdate").json()
    assert body["ErrorNumber"] == 1026


def test_refresh_is_a_put_noop(client: TestClient) -> None:
    body = client.put("/api/v1/observingconditions/0/refresh").json()
    assert body["ErrorNumber"] == 0
    assert "Value" not in body


def test_devicestate(client: TestClient) -> None:
    safety = client.get("/api/v1/safetymonitor/0/devicestate").json()["Value"]
    assert {"Name": "IsSafe", "Value": True} in safety
    assert any(entry["Name"] == "TimeStamp" for entry in safety)

    weather = client.get("/api/v1/observingconditions/0/devicestate").json()["Value"]
    names = [entry["Name"] for entry in weather]
    assert "Temperature" in names and "Humidity" in names
    assert "CloudCover" not in names  # not implemented upstream
    assert "TimeStamp" in names


# --- common device methods ----------------------------------------------


@pytest.mark.parametrize("device_type", ["safetymonitor", "observingconditions"])
def test_connected_put_then_get(client: TestClient, device_type: str) -> None:
    url = f"/api/v1/{device_type}/0/connected"
    assert client.get(url).json()["Value"] is False

    put = client.put(url, data={"Connected": "True", "ClientTransactionID": "5"}).json()
    assert put["ErrorNumber"] == 0
    assert put["ClientTransactionID"] == 5
    assert "Value" not in put

    assert client.get(url).json()["Value"] is True

    client.put(url, data={"connected": "false"})
    assert client.get(url).json()["Value"] is False


def test_property_reads_ignore_connected(client: TestClient) -> None:
    # Connected is False by default; reads must still be served.
    assert client.get("/api/v1/safetymonitor/0/issafe").json()["ErrorNumber"] == 0


@pytest.mark.parametrize(
    "device_type,version", [("safetymonitor", 3), ("observingconditions", 2)]
)
def test_interfaceversion(client: TestClient, device_type: str, version: int) -> None:
    url = f"/api/v1/{device_type}/0/interfaceversion"
    assert client.get(url).json()["Value"] == version


@pytest.mark.parametrize("device_type", ["safetymonitor", "observingconditions"])
def test_supportedactions_and_connecting(client: TestClient, device_type: str) -> None:
    base = f"/api/v1/{device_type}/0"
    assert client.get(f"{base}/supportedactions").json()["Value"] == []
    assert client.get(f"{base}/connecting").json()["Value"] is False
    assert client.put(f"{base}/connect").json()["ErrorNumber"] == 0
    assert client.put(f"{base}/disconnect").json()["ErrorNumber"] == 0


@pytest.mark.parametrize("method", ["action", "commandblind", "commandbool", "commandstring"])
def test_command_methods_are_1024(client: TestClient, method: str) -> None:
    assert client.put(f"/api/v1/safetymonitor/0/{method}").json()["ErrorNumber"] == 1024


def test_name_and_description_come_from_config(client: TestClient) -> None:
    assert client.get("/api/v1/safetymonitor/0/name").json()["Value"] == "Test Safety Monitor"
    assert (
        client.get("/api/v1/observingconditions/0/description").json()["Value"]
        == "Test Weather Station"
    )


# --- error numbers -------------------------------------------------------

# 0x407 is ASCOM NotConnected. Emitting it tells NINA the device has dropped,
# which is the spurious equipment fault this proxy exists to prevent, so these
# tests pin the exact values rather than merely checking for a non-zero error.
def test_error_constants_are_the_ascom_values() -> None:
    assert server.ERR_NOT_IMPLEMENTED == 0x400
    assert server.ERR_INVALID_VALUE == 0x401
    assert server.ERR_VALUE_NOT_SET == 0x402
    assert server.ERR_DRIVER_BASE == 0x500
    # None of them may be NotConnected.
    assert 0x407 not in vars(server).values()


def test_value_not_set_weather_read_is_value_not_set(client: TestClient) -> None:
    body = client.get("/api/v1/observingconditions/0/pressure").json()
    assert body["ErrorNumber"] == 0x402


def test_catch_all_is_driver_base_not_not_connected(
    client: TestClient, poller: FakePoller
) -> None:
    poller.explode = True
    body = client.get("/api/v1/safetymonitor/0/issafe").json()
    assert body["ErrorNumber"] == 0x500
    assert "internal proxy error" in body["ErrorMessage"]
