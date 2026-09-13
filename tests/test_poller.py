from __future__ import annotations

import asyncio
from typing import Any, Callable

import httpx
import pytest

from alpaca_proxy.config import Config, DeviceConfig
from alpaca_proxy.poller import WEATHER_PROPERTIES, Poller, ValueNotSet
from alpaca_proxy.upstream import UpstreamClient, UpstreamError, UpstreamNotImplemented

SAFETY = DeviceConfig(
    device_type="safetymonitor",
    upstream_device_number=8,
    local_device_number=0,
    name="Test Safety Monitor",
)
WEATHER = DeviceConfig(
    device_type="observingconditions",
    upstream_device_number=1,
    local_device_number=0,
    name="Test Weather Station",
)


def make_config(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 11111,
        "poll_interval_seconds": 0.01,
        "failure_threshold": 3,
        "request_timeout_seconds": 1.0,
        "unsafe_on_weather_failure": True,
        "upstream_base_url": "https://upstream.test",
        "client_id": 1,
        "log_file": None,
        "log_level": "INFO",
        "safety": SAFETY,
        "weather": WEATHER,
    }
    base.update(overrides)
    return Config(**base)


class FakeClient:
    """Stands in for UpstreamClient. `behaviour` maps method -> value or exception."""

    def __init__(self) -> None:
        self.behaviour: dict[str, Any] = {"issafe": True}
        self.default: Any = 1.0
        self.calls: list[tuple[str, int, str]] = []

    async def get(self, device_type: str, device_number: int, method: str) -> Any:
        self.calls.append((device_type, device_number, method))
        value = self.behaviour.get(method, self.default)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value()
        return value

    async def aclose(self) -> None:  # pragma: no cover - never used in tests
        pass

    def fail_all(self) -> None:
        self.behaviour = {}
        self.default = UpstreamError("boom")

    def heal(self, issafe: bool = True) -> None:
        self.behaviour = {"issafe": issafe}
        self.default = 1.0


def make_poller(**config_overrides: Any) -> tuple[Poller, FakeClient]:
    client = FakeClient()
    return Poller(make_config(**config_overrides), client), client  # type: ignore[arg-type]


# -- UpstreamClient ----------------------------------------------------------


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> UpstreamClient:
    return UpstreamClient(
        "https://upstream.test/",
        client_id=7,
        timeout=1.0,
        transport=httpx.MockTransport(handler),
    )


def envelope(value: Any = None, error_number: int = 0, message: str = "") -> dict:
    body: dict[str, Any] = {
        "ClientTransactionID": 1,
        "ServerTransactionID": 1,
        "ErrorNumber": error_number,
        "ErrorMessage": message,
    }
    if error_number == 0:
        body["Value"] = value
    return body


@pytest.mark.asyncio
async def test_upstream_success_and_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=envelope(True))

    client = mock_client(handler)
    assert await client.get("safetymonitor", 8, "issafe") is True
    assert await client.get("safetymonitor", 8, "issafe") is True
    await client.aclose()

    assert seen[0].url.path == "/api/v1/safetymonitor/8/issafe"
    assert seen[0].url.params["ClientID"] == "7"
    # ClientTransactionID increments per request.
    assert int(seen[1].url.params["ClientTransactionID"]) == int(
        seen[0].url.params["ClientTransactionID"]
    ) + 1


@pytest.mark.asyncio
async def test_upstream_not_implemented() -> None:
    client = mock_client(
        lambda request: httpx.Response(200, json=envelope(error_number=1024, message="not implemented"))
    )
    with pytest.raises(UpstreamNotImplemented):
        await client.get("observingconditions", 1, "cloudcover")
    await client.aclose()


def test_not_implemented_is_not_an_upstream_error() -> None:
    # The poller relies on these being unrelated exception types.
    assert not issubclass(UpstreamNotImplemented, UpstreamError)


@pytest.mark.asyncio
async def test_upstream_other_error_number() -> None:
    client = mock_client(
        lambda request: httpx.Response(200, json=envelope(error_number=1031, message="value not set"))
    )
    with pytest.raises(UpstreamError, match="value not set"):
        await client.get("observingconditions", 1, "temperature")
    await client.aclose()


@pytest.mark.asyncio
async def test_upstream_http_500() -> None:
    client = mock_client(lambda request: httpx.Response(500, text="nope"))
    with pytest.raises(UpstreamError, match="HTTP 500"):
        await client.get("safetymonitor", 8, "issafe")
    await client.aclose()


@pytest.mark.asyncio
async def test_upstream_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = mock_client(handler)
    with pytest.raises(UpstreamError, match="failed"):
        await client.get("safetymonitor", 8, "issafe")
    await client.aclose()


@pytest.mark.asyncio
async def test_upstream_non_json_body() -> None:
    client = mock_client(lambda request: httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(UpstreamError, match="not JSON"):
        await client.get("safetymonitor", 8, "issafe")
    await client.aclose()


@pytest.mark.asyncio
async def test_upstream_json_not_an_object() -> None:
    client = mock_client(lambda request: httpx.Response(200, json=[1, 2, 3]))
    with pytest.raises(UpstreamError, match="not a JSON object"):
        await client.get("safetymonitor", 8, "issafe")
    await client.aclose()


@pytest.mark.asyncio
async def test_upstream_missing_value() -> None:
    client = mock_client(
        lambda request: httpx.Response(200, json={"ErrorNumber": 0, "ErrorMessage": ""})
    )
    with pytest.raises(UpstreamError, match="no Value"):
        await client.get("safetymonitor", 8, "issafe")
    await client.aclose()


# -- Poller: happy path ------------------------------------------------------


def test_weather_properties_are_the_expected_fourteen() -> None:
    assert len(WEATHER_PROPERTIES) == 14
    assert len(set(WEATHER_PROPERTIES)) == 14
    for method in ("cloudcover", "skyquality", "starfwhm", "temperature", "humidity"):
        assert method in WEATHER_PROPERTIES


@pytest.mark.asyncio
async def test_cold_start_is_unsafe() -> None:
    poller, _ = make_poller()
    assert poller.is_safe() is False
    assert poller.safety.has_data is False


@pytest.mark.asyncio
async def test_successful_poll_caches_everything() -> None:
    poller, client = make_poller()
    client.behaviour = {"issafe": True, "temperature": 12.5}
    await poller.poll_once()

    assert poller.is_safe() is True
    assert poller.safety.last_success_utc is not None
    assert poller.weather_value("temperature") == 12.5
    assert set(poller.weather.values) == set(WEATHER_PROPERTIES)
    assert client.calls[0] == ("safetymonitor", 8, "issafe")
    assert all(call[0] == "observingconditions" for call in client.calls[1:])


@pytest.mark.asyncio
async def test_issafe_false_is_served_verbatim() -> None:
    poller, client = make_poller()
    client.behaviour = {"issafe": False}
    await poller.poll_once()
    assert poller.is_safe() is False


@pytest.mark.asyncio
async def test_weather_value_not_set_before_first_poll() -> None:
    poller, _ = make_poller()
    with pytest.raises(ValueNotSet):
        poller.weather_value("temperature")


# -- Poller: 1024 passthrough ------------------------------------------------


@pytest.mark.asyncio
async def test_not_implemented_is_not_a_failure() -> None:
    poller, client = make_poller()
    client.behaviour = {
        "issafe": True,
        "cloudcover": UpstreamNotImplemented("nope"),
        "skyquality": UpstreamNotImplemented("nope"),
        "starfwhm": UpstreamNotImplemented("nope"),
    }
    await poller.poll_once()

    assert poller.weather.consecutive_failures == 0
    assert poller.is_safe() is True
    assert poller.weather.not_implemented == {"cloudcover", "skyquality", "starfwhm"}
    with pytest.raises(UpstreamNotImplemented):
        poller.weather_value("cloudcover")

    # Subsequent cycles skip the known-unimplemented properties.
    client.calls.clear()
    await poller.poll_once()
    polled = {call[2] for call in client.calls}
    assert "cloudcover" not in polled
    assert "temperature" in polled


@pytest.mark.asyncio
async def test_not_implemented_relearned_after_a_failed_cycle() -> None:
    poller, client = make_poller()
    client.behaviour = {"issafe": True, "cloudcover": UpstreamNotImplemented("nope")}
    await poller.poll_once()
    assert poller.weather.not_implemented == {"cloudcover"}

    client.fail_all()
    await poller.poll_once()
    client.heal()
    await poller.poll_once()

    assert poller.weather.not_implemented == set()
    assert poller.weather_value("cloudcover") == 1.0


# -- Poller: failure counting ------------------------------------------------


@pytest.mark.asyncio
async def test_failures_count_and_cross_threshold_exactly() -> None:
    poller, client = make_poller(failure_threshold=3)
    client.heal()
    await poller.poll_once()
    assert poller.is_safe() is True

    client.fail_all()
    for expected in (1, 2):
        await poller.poll_once()
        assert poller.safety.consecutive_failures == expected
        assert poller.safety.failed is False
        assert poller.is_safe() is True, "must stay safe below the threshold"

    await poller.poll_once()
    assert poller.safety.consecutive_failures == 3
    assert poller.safety.failed is True
    assert poller.is_safe() is False


@pytest.mark.asyncio
async def test_cached_values_survive_a_failed_cycle() -> None:
    poller, client = make_poller()
    client.behaviour = {"issafe": True, "temperature": 9.0}
    await poller.poll_once()

    client.fail_all()
    await poller.poll_once()

    assert poller.weather_value("temperature") == 9.0
    assert poller.safety.values["issafe"] is True
    assert poller.weather.last_success_utc is not None


@pytest.mark.asyncio
async def test_partial_weather_failure_fails_the_whole_cycle() -> None:
    poller, client = make_poller()
    client.behaviour = {"issafe": True, "humidity": UpstreamError("flaky")}
    await poller.poll_once()

    assert poller.weather.consecutive_failures == 1
    assert poller.safety.consecutive_failures == 0
    # Cache untouched: nothing was committed from the bad cycle.
    assert poller.weather.values == {}


@pytest.mark.asyncio
async def test_recovery_is_not_latching() -> None:
    poller, client = make_poller(failure_threshold=2)
    client.heal()
    await poller.poll_once()

    client.fail_all()
    await poller.poll_once()
    await poller.poll_once()
    assert poller.is_safe() is False

    client.heal(issafe=True)
    await poller.poll_once()
    assert poller.safety.consecutive_failures == 0
    assert poller.safety.failed is False
    assert poller.is_safe() is True


# -- Poller: weather-driven safety ------------------------------------------


@pytest.mark.asyncio
async def test_weather_failure_drives_unsafe_when_enabled() -> None:
    poller, client = make_poller(failure_threshold=2, unsafe_on_weather_failure=True)
    client.heal()
    await poller.poll_once()

    client.behaviour = {"issafe": True}
    client.default = UpstreamError("weather down")
    await poller.poll_once()
    assert poller.is_safe() is True, "one weather failure is below the threshold"
    await poller.poll_once()

    assert poller.safety.consecutive_failures == 0
    assert poller.weather.failed is True
    assert poller.is_safe() is False


@pytest.mark.asyncio
async def test_weather_failure_ignored_when_disabled() -> None:
    poller, client = make_poller(failure_threshold=2, unsafe_on_weather_failure=False)
    client.heal()
    await poller.poll_once()

    client.behaviour = {"issafe": True}
    client.default = UpstreamError("weather down")
    await poller.poll_once()
    await poller.poll_once()

    assert poller.weather.failed is True
    assert poller.is_safe() is True


# -- Poller: status and run --------------------------------------------------


@pytest.mark.asyncio
async def test_status_is_json_serialisable() -> None:
    import json

    poller, client = make_poller()
    client.behaviour = {"issafe": True, "cloudcover": UpstreamNotImplemented("nope")}
    await poller.poll_once()
    client.fail_all()
    await poller.poll_once()

    status = poller.status()
    json.dumps(status)  # must not raise

    assert status["is_safe"] is True
    assert status["safety"]["consecutive_failures"] == 1
    assert status["safety"]["failure_threshold"] == 3
    assert status["safety"]["failed"] is False
    assert status["safety"]["last_success_utc"].endswith("+00:00")
    assert status["weather"]["not_implemented"] == ["cloudcover"]
    assert status["weather"]["values"]["temperature"] == 1.0


@pytest.mark.asyncio
async def test_run_survives_unexpected_errors_and_stops_on_cancel() -> None:
    poller, client = make_poller()
    calls = {"n": 0}
    original = poller.poll_once

    async def flaky() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("unexpected")
        await original()

    poller.poll_once = flaky  # type: ignore[method-assign]
    task = asyncio.create_task(poller.run())
    for _ in range(200):
        await asyncio.sleep(0.005)
        if calls["n"] >= 3:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls["n"] >= 3, "loop kept running after an unexpected exception"
    assert poller.safety.has_data is True
