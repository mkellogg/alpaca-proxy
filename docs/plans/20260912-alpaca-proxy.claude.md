# Alpaca Proxy — implementation plan

Companion to `20260912-alpaca-proxy.md`.

## Stack

Python 3.11+, FastAPI + uvicorn (server), httpx (upstream), PyYAML (config), pytest +
pytest-asyncio (tests). No other runtime dependencies.

## Layout and file ownership

Two agents work in parallel against the frozen contract below. Ownership is strictly
disjoint; neither touches the other's files.

```
alpaca-proxy/
├── alpaca_proxy/
│   ├── __init__.py        B
│   ├── __main__.py        B   entrypoint, arg parsing, logging setup, wiring
│   ├── config.py          A   YAML -> dataclasses + validation
│   ├── upstream.py        A   httpx Alpaca client
│   ├── poller.py          A   poll loop, cache, failure counters, is_safe()
│   └── server.py          B   FastAPI app, Alpaca protocol surface
├── tests/
│   ├── test_config.py     A
│   ├── test_poller.py     A
│   └── test_server.py     B
├── config.example.yaml    A
├── pyproject.toml         B
├── README.md              B
└── install-service.ps1    B
```

## Frozen contract

Agent B codes against these signatures without reading A's implementation.

```python
# config.py
@dataclass(frozen=True)
class DeviceConfig:
    device_type: str          # "safetymonitor" | "observingconditions"
    upstream_device_number: int
    local_device_number: int
    name: str

@dataclass(frozen=True)
class Config:
    host: str; port: int
    poll_interval_seconds: float
    failure_threshold: int
    request_timeout_seconds: float
    unsafe_on_weather_failure: bool
    upstream_base_url: str            # no trailing slash
    client_id: int
    log_file: str | None; log_level: str
    safety: DeviceConfig
    weather: DeviceConfig

def load_config(path: str | Path) -> Config: ...   # raises ValueError on bad config

# upstream.py
class UpstreamError(Exception): ...           # transport/protocol -> counts as a failure
class UpstreamNotImplemented(Exception): ...  # ErrorNumber 1024 -> NOT a failure

class UpstreamClient:
    def __init__(self, base_url: str, client_id: int, timeout: float) -> None: ...
    async def get(self, device_type: str, device_number: int, method: str) -> Any: ...
    async def aclose(self) -> None: ...

# poller.py
class ValueNotSet(Exception): ...

WEATHER_PROPERTIES: tuple[str, ...]   # the 14 ObservingConditions properties

class DeviceState:
    consecutive_failures: int
    last_success_utc: datetime | None
    values: dict[str, Any]
    not_implemented: set[str]
    @property
    def failed(self) -> bool          # consecutive_failures >= threshold
    @property
    def has_data(self) -> bool        # last_success_utc is not None

class Poller:
    def __init__(self, config: Config, client: UpstreamClient) -> None: ...
    safety: DeviceState
    weather: DeviceState
    async def poll_once(self) -> None
    async def run(self) -> None       # loop; never raises out
    def is_safe(self) -> bool
    def weather_value(self, method: str) -> Any   # raises UpstreamNotImplemented / ValueNotSet
    def status(self) -> dict
```

`is_safe()`:

```
not safety.has_data                                   -> False   (fail safe on cold start)
safety.failed                                         -> False
unsafe_on_weather_failure and weather.failed          -> False
otherwise                                             -> bool(safety.values["issafe"])
```

## Alpaca surface (agent B)

```
GET  /management/apiversions            -> {"Value":[1],...}
GET  /management/v1/description         -> proxy's own identity
GET  /management/v1/configureddevices   -> the two local devices
GET  /api/v1/{device_type}/{device_number}/{method}
PUT  /api/v1/{device_type}/{device_number}/{method}
GET  /status                            -> Poller.status(), for debugging
```

Envelope rules:

- Always HTTP 200. Success: `{"Value":..,"ClientTransactionID":N,"ServerTransactionID":M,
  "ErrorNumber":0,"ErrorMessage":""}`. Errors carry `ErrorNumber`/`ErrorMessage` and no
  `Value`.
- `ServerTransactionID` is a process-wide monotonic counter.
- `ClientTransactionID` echoes the request's, `0` when absent or unparseable.
- **Query and form parameter names are case-insensitive** per the Alpaca spec — NINA sends
  `ClientID` / `ClientTransactionID`. Do not rely on FastAPI's case-sensitive binding.
- Unknown device type / device number -> `ErrorNumber 1025` ("invalid device").
- Unknown method -> `ErrorNumber 1024`.
- `UpstreamNotImplemented` -> `1024 "not implemented"`. `ValueNotSet` -> `1026`.
- Unexpected internal exception -> `1280` (`0x500`, base of the driver-defined range),
  never a 5xx.

Verified ASCOM error numbers, for reference — an earlier draft of this plan wrongly used
`1031` for ValueNotSet, which is actually NotConnected and would have made NINA believe the
device had disconnected:

```
0x400 / 1024  NotImplemented
0x401 / 1025  InvalidValue
0x402 / 1026  ValueNotSet
0x407 / 1031  NotConnected      <- do not use
0x500+ / 1280 driver-defined range
```

Device numbers exposed locally come from config (`local_device_number`). `UniqueID` in
configureddevices is a deterministic `uuid5(NAMESPACE_URL, f"{base}/{type}/{upstream_n}")`
so it is stable without contacting upstream at startup.

Per-device methods:

- both: `connected` (GET/PUT), `connecting` (GET, always false), `connect`/`disconnect`
  (PUT, no-op), `name`, `description`, `driverinfo`, `driverversion`, `interfaceversion`,
  `supportedactions` (`[]`), `devicestate`, `action`/`commandblind`/`commandbool`/
  `commandstring` (-> 1024)
- safetymonitor: `issafe` -> `Poller.is_safe()`
- observingconditions: the 14 properties -> `Poller.weather_value()`, plus
  `refresh` (PUT, no-op), `sensordescription` (GET), `timesincelastupdate` (GET, seconds
  since `weather.last_success_utc`)

`interfaceversion` mirrors upstream: safetymonitor 3, observingconditions 2.
`connected` is a stored bool per device, initial `False`, set by PUT. Property reads are
served regardless of its value.

## Decisions and trade-offs

- **Weather failure drives IsSafe.** Both devices are behind one host, so a weather-only
  failure almost always means the host is unreachable. Config flag
  `unsafe_on_weather_failure` (default true) exists to decouple them.
- **Cold start is unsafe.** Before the first successful poll `IsSafe` is false. The
  alternative — optimistic true — would let NINA open up against an upstream that has
  never answered.
- **1024 discovery is per-poll, not cached at startup.** Properties in
  `not_implemented` are skipped on subsequent polls but the set is re-learned if the
  device ever answers them, avoiding a permanent wrong answer from one bad startup.
- **No latching on recovery**, per the spec. One good poll clears the unsafe state.
- **Scheduled Task, not NSSM**, for the Windows service — native, nothing extra to
  install.
