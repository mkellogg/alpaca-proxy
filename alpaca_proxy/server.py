"""FastAPI application implementing the local Alpaca surface.

Every /api/v1 and /management response is HTTP 200 carrying an Alpaca error
envelope. Emitting a 5xx would surface in NINA as a device fault, which is the
exact failure this proxy exists to prevent, so the dispatch is wrapped in a
catch-all that converts any unexpected exception into an envelope.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl
from uuid import NAMESPACE_URL, uuid5

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .config import Config, DeviceConfig
from .poller import WEATHER_PROPERTIES, Poller, ValueNotSet
from .upstream import UpstreamNotImplemented

log = logging.getLogger(__name__)

ERR_NOT_IMPLEMENTED = 1024
ERR_INVALID_VALUE = 1025
ERR_VALUE_NOT_SET = 1026
# 0x500 is the base of the ASCOM driver-defined error range. Note that 0x407
# is NotConnected and must never be emitted here: it tells NINA the device
# has dropped, which is the spurious equipment fault this proxy prevents.
ERR_DRIVER_BASE = 1280

SERVER_NAME = "Alpaca Proxy"
MANUFACTURER = "alpaca-proxy"
LOCATION = "Local cache in front of the upstream Alpaca API"

INTERFACE_VERSIONS: dict[str, int] = {
    "safetymonitor": 3,
    "observingconditions": 2,
}

DEVICE_TYPE_DISPLAY: dict[str, str] = {
    "safetymonitor": "SafetyMonitor",
    "observingconditions": "ObservingConditions",
}

# Alpaca-cased names for the ObservingConditions properties, used by devicestate.
WEATHER_DISPLAY_NAMES: dict[str, str] = {
    "averageperiod": "AveragePeriod",
    "cloudcover": "CloudCover",
    "dewpoint": "DewPoint",
    "humidity": "Humidity",
    "pressure": "Pressure",
    "rainrate": "RainRate",
    "skybrightness": "SkyBrightness",
    "skyquality": "SkyQuality",
    "skytemperature": "SkyTemperature",
    "starfwhm": "StarFWHM",
    "temperature": "Temperature",
    "winddirection": "WindDirection",
    "windgust": "WindGust",
    "windspeed": "WindSpeed",
}

# AveragePeriod is a setting, not a sensor, so it is deliberately absent here.
SENSOR_DESCRIPTIONS: dict[str, str] = {
    "cloudcover": "Cloud cover, percent",
    "dewpoint": "Dew point, degrees C",
    "humidity": "Relative humidity, percent",
    "pressure": "Barometric pressure, hPa",
    "rainrate": "Rain rate, mm per hour",
    "skybrightness": "Sky brightness, lux",
    "skyquality": "Sky quality, magnitudes per square arcsecond",
    "skytemperature": "Sky temperature, degrees C",
    "starfwhm": "Star FWHM, arcseconds",
    "temperature": "Ambient temperature, degrees C",
    "winddirection": "Wind direction, degrees east of north",
    "windgust": "Wind gust, metres per second",
    "windspeed": "Wind speed, metres per second",
}

# Alpaca requires ServerTransactionID to be monotonic for the life of the process.
_server_transaction_ids = itertools.count(1)


class _NoValue:
    """Sentinel for PUT handlers, whose envelopes carry no Value member."""


NO_VALUE = _NoValue()

Handler = Callable[["Params"], Any]


class AlpacaError(Exception):
    """An error to report inside the envelope rather than as an HTTP status."""

    def __init__(self, number: int, message: str) -> None:
        super().__init__(message)
        self.number = number
        self.message = message


class Params:
    """Case-insensitive view over query/form parameters.

    The Alpaca spec makes parameter names case-insensitive. NINA sends
    ``ClientID``/``ClientTransactionID``; conformance tools send ``clientid``.
    """

    def __init__(self, pairs: Iterable[tuple[str, Any]] = ()) -> None:
        self._values: dict[str, Any] = {str(k).lower(): v for k, v in pairs}

    def get(self, name: str, default: Any = None) -> Any:
        return self._values.get(name.lower(), default)

    def __contains__(self, name: object) -> bool:
        return str(name).lower() in self._values


def _query_params(request: Request) -> Params:
    return Params(request.query_params.multi_items())


async def _get_params(request: Request) -> Params:
    return _query_params(request)


async def _put_params(request: Request) -> Params:
    """Merge query params with the urlencoded body, body winning.

    Parsed by hand rather than via ``request.form()`` so the proxy does not need
    the ``python-multipart`` dependency; Alpaca PUTs are always urlencoded.
    """
    pairs: list[tuple[str, Any]] = list(request.query_params.multi_items())
    body = await request.body()
    if body:
        content_type = request.headers.get("content-type", "")
        if "multipart/" not in content_type:
            text = body.decode("utf-8", errors="replace")
            pairs.extend(parse_qsl(text, keep_blank_values=True))
    return Params(pairs)


def _client_transaction_id(params: Params) -> int:
    """Echo the caller's ClientTransactionID; 0 if absent, negative or garbage."""
    try:
        value = int(str(params.get("ClientTransactionID")).strip())
    except (TypeError, ValueError):
        return 0
    return value if value >= 0 else 0


def _parse_bool(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _timestamp(moment: datetime | None) -> str:
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _envelope(
    client_transaction_id: int,
    value: Any = NO_VALUE,
    error_number: int = 0,
    error_message: str = "",
) -> JSONResponse:
    body: dict[str, Any] = {}
    if error_number == 0 and value is not NO_VALUE:
        body["Value"] = value
    body["ClientTransactionID"] = client_transaction_id
    body["ServerTransactionID"] = next(_server_transaction_ids)
    body["ErrorNumber"] = error_number
    body["ErrorMessage"] = error_message
    return JSONResponse(body)


def _unique_id(base_url: str, device: DeviceConfig) -> str:
    """Stable per-device UUID, derived without contacting upstream."""
    seed = f"{base_url}/{device.device_type}/{device.upstream_device_number}"
    return str(uuid5(NAMESPACE_URL, seed))


def create_app(
    config: Config,
    poller: Poller,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    """Build the proxy's FastAPI app.

    ``lifespan`` is optional so tests can build the app without starting the
    poll loop; ``__main__`` passes one that owns the background poll task.
    """
    app = FastAPI(title=SERVER_NAME, version=__version__, lifespan=lifespan)

    devices: dict[str, DeviceConfig] = {
        config.safety.device_type: config.safety,
        config.weather.device_type: config.weather,
    }
    connected: dict[str, bool] = {device_type: False for device_type in devices}

    # --- shared handlers -------------------------------------------------

    def common_get(device: DeviceConfig) -> dict[str, Handler]:
        device_type = device.device_type
        return {
            "connected": lambda params: connected[device_type],
            "connecting": lambda params: False,
            "name": lambda params: device.name,
            "description": lambda params: device.name,
            "driverinfo": lambda params: f"{SERVER_NAME} {__version__}, cached {device_type}",
            "driverversion": lambda params: __version__,
            "interfaceversion": lambda params: INTERFACE_VERSIONS[device_type],
            "supportedactions": lambda params: [],
        }

    def unimplemented(params: Params) -> Any:
        raise AlpacaError(ERR_NOT_IMPLEMENTED, "not implemented")

    def common_put(device: DeviceConfig) -> dict[str, Handler]:
        device_type = device.device_type

        def set_connected(params: Params) -> Any:
            connected[device_type] = _parse_bool(params.get("Connected"))
            log.info("%s Connected set to %s", device_type, connected[device_type])
            return NO_VALUE

        return {
            "connected": set_connected,
            "connect": lambda params: NO_VALUE,
            "disconnect": lambda params: NO_VALUE,
            "action": unimplemented,
            "commandblind": unimplemented,
            "commandbool": unimplemented,
            "commandstring": unimplemented,
        }

    # --- safetymonitor ---------------------------------------------------

    def safety_devicestate(params: Params) -> Any:
        return [
            {"Name": "IsSafe", "Value": poller.is_safe()},
            {"Name": "TimeStamp", "Value": _timestamp(poller.safety.last_success_utc)},
        ]

    safety_get: dict[str, Handler] = {
        **common_get(config.safety),
        "issafe": lambda params: poller.is_safe(),
        "devicestate": safety_devicestate,
    }
    safety_put: dict[str, Handler] = common_put(config.safety)

    # --- observingconditions ---------------------------------------------

    def weather_reader(prop: str) -> Handler:
        return lambda params: poller.weather_value(prop)

    def sensor_description(params: Params) -> Any:
        sensor = params.get("SensorName")
        description = SENSOR_DESCRIPTIONS.get(str(sensor).strip().lower())
        if description is None:
            raise AlpacaError(ERR_INVALID_VALUE, f"unknown sensor {sensor!r}")
        return description

    def time_since_last_update(params: Params) -> Any:
        last = poller.weather.last_success_utc
        if last is None:
            raise ValueNotSet("no successful weather poll yet")
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds()

    def weather_devicestate(params: Params) -> Any:
        state: list[dict[str, Any]] = []
        for prop in WEATHER_PROPERTIES:
            try:
                value = poller.weather_value(prop)
            except (UpstreamNotImplemented, ValueNotSet):
                continue
            state.append({"Name": WEATHER_DISPLAY_NAMES.get(prop, prop), "Value": value})
        state.append({"Name": "TimeStamp", "Value": _timestamp(poller.weather.last_success_utc)})
        return state

    weather_get: dict[str, Handler] = {
        **common_get(config.weather),
        **{prop: weather_reader(prop) for prop in WEATHER_PROPERTIES},
        "sensordescription": sensor_description,
        "timesincelastupdate": time_since_last_update,
        "devicestate": weather_devicestate,
    }
    weather_put: dict[str, Handler] = {
        **common_put(config.weather),
        "refresh": lambda params: NO_VALUE,
    }

    get_tables: dict[str, dict[str, Handler]] = {
        config.safety.device_type: safety_get,
        config.weather.device_type: weather_get,
    }
    put_tables: dict[str, dict[str, Handler]] = {
        config.safety.device_type: safety_put,
        config.weather.device_type: weather_put,
    }

    def resolve(
        tables: dict[str, dict[str, Handler]],
        device_type: str,
        device_number: str,
        method: str,
    ) -> Handler:
        key = device_type.strip().lower()
        device = devices.get(key)
        if device is None:
            raise AlpacaError(ERR_INVALID_VALUE, f"unknown device type '{device_type}'")
        try:
            number = int(device_number)
        except (TypeError, ValueError):
            raise AlpacaError(
                ERR_INVALID_VALUE, f"invalid device number '{device_number}'"
            ) from None
        if number != device.local_device_number:
            raise AlpacaError(ERR_INVALID_VALUE, f"no {key} with device number {number}")
        handler = tables[key].get(method.strip().lower())
        if handler is None:
            raise AlpacaError(ERR_NOT_IMPLEMENTED, f"'{method}' is not implemented")
        return handler

    async def dispatch(
        request: Request,
        tables: dict[str, dict[str, Handler]],
        device_type: str,
        device_number: str,
        method: str,
        read_params: Callable[[Request], Awaitable[Params]],
    ) -> JSONResponse:
        client_transaction_id = 0
        try:
            params = await read_params(request)
            client_transaction_id = _client_transaction_id(params)
            value = resolve(tables, device_type, device_number, method)(params)
        except AlpacaError as exc:
            return _envelope(
                client_transaction_id,
                error_number=exc.number,
                error_message=exc.message,
            )
        except UpstreamNotImplemented:
            return _envelope(
                client_transaction_id,
                error_number=ERR_NOT_IMPLEMENTED,
                error_message="not implemented",
            )
        except ValueNotSet:
            return _envelope(
                client_transaction_id,
                error_number=ERR_VALUE_NOT_SET,
                error_message="no value has been read from upstream yet",
            )
        except Exception as exc:  # never let a 5xx reach NINA
            log.exception(
                "unhandled error serving %s %s/%s/%s",
                request.method,
                device_type,
                device_number,
                method,
            )
            return _envelope(
                client_transaction_id,
                error_number=ERR_DRIVER_BASE,
                error_message=f"internal proxy error: {exc}",
            )
        return _envelope(client_transaction_id, value=value)

    # --- routes ----------------------------------------------------------

    @app.get("/api/v1/{device_type}/{device_number}/{method}")
    async def alpaca_get(
        request: Request, device_type: str, device_number: str, method: str
    ) -> JSONResponse:
        return await dispatch(
            request, get_tables, device_type, device_number, method, _get_params
        )

    @app.put("/api/v1/{device_type}/{device_number}/{method}")
    async def alpaca_put(
        request: Request, device_type: str, device_number: str, method: str
    ) -> JSONResponse:
        return await dispatch(
            request, put_tables, device_type, device_number, method, _put_params
        )

    @app.get("/management/apiversions")
    async def api_versions(request: Request) -> JSONResponse:
        return _envelope(_client_transaction_id(_query_params(request)), value=[1])

    @app.get("/management/v1/description")
    async def description(request: Request) -> JSONResponse:
        return _envelope(
            _client_transaction_id(_query_params(request)),
            value={
                "ServerName": SERVER_NAME,
                "Manufacturer": MANUFACTURER,
                "ManufacturerVersion": __version__,
                "Location": LOCATION,
            },
        )

    @app.get("/management/v1/configureddevices")
    async def configured_devices(request: Request) -> JSONResponse:
        value = [
            {
                "DeviceName": device.name,
                "DeviceType": DEVICE_TYPE_DISPLAY.get(
                    device.device_type, device.device_type
                ),
                "DeviceNumber": device.local_device_number,
                "UniqueID": _unique_id(config.upstream_base_url, device),
            }
            for device in (config.safety, config.weather)
        ]
        return _envelope(_client_transaction_id(_query_params(request)), value=value)

    @app.get("/status")
    async def status() -> Mapping[str, Any]:
        return poller.status()

    return app
