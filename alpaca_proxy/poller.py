"""Background poll loop, value cache and failure counters."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from .config import Config, DeviceConfig
from .upstream import UpstreamClient, UpstreamError, UpstreamNotImplemented

logger = logging.getLogger(__name__)

SAFETY_PROPERTY = "issafe"

WEATHER_PROPERTIES: tuple[str, ...] = (
    "averageperiod",
    "cloudcover",
    "dewpoint",
    "humidity",
    "pressure",
    "rainrate",
    "skybrightness",
    "skyquality",
    "skytemperature",
    "starfwhm",
    "temperature",
    "winddirection",
    "windgust",
    "windspeed",
)


class ValueNotSet(Exception):
    """No cached value is available yet (Alpaca ErrorNumber 0x402 / ValueNotSet)."""


class DeviceState:
    """Cache and failure counters for one proxied device."""

    def __init__(self, threshold: int) -> None:
        self.threshold = threshold
        self.consecutive_failures: int = 0
        self.last_success_utc: datetime | None = None
        self.values: dict[str, Any] = {}
        self.not_implemented: set[str] = set()

    @property
    def failed(self) -> bool:
        return self.consecutive_failures >= self.threshold

    @property
    def has_data(self) -> bool:
        return self.last_success_utc is not None


class Poller:
    """Polls upstream on a timer and answers from cache."""

    def __init__(self, config: Config, client: UpstreamClient) -> None:
        self._config = config
        self._client = client
        self.safety = DeviceState(config.failure_threshold)
        self.weather = DeviceState(config.failure_threshold)

    async def poll_once(self) -> None:
        """Run one poll cycle for both devices. Never raises UpstreamError."""
        await self._poll_device(self.safety, self._config.safety, (SAFETY_PROPERTY,))
        await self._poll_device(self.weather, self._config.weather, WEATHER_PROPERTIES)

    async def run(self) -> None:
        """Poll forever. Only cancellation escapes this loop."""
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                logger.info("poll loop cancelled")
                raise
            except Exception:  # noqa: BLE001 - the loop must survive anything
                logger.exception("unexpected error during poll cycle")
            await asyncio.sleep(self._config.poll_interval_seconds)

    def is_safe(self) -> bool:
        if not self.safety.has_data:
            return False
        if self.safety.failed:
            return False
        if self._config.unsafe_on_weather_failure and self.weather.failed:
            return False
        return bool(self.safety.values.get(SAFETY_PROPERTY, False))

    def weather_value(self, method: str) -> Any:
        if method in self.weather.not_implemented:
            raise UpstreamNotImplemented(f"{method}: not implemented upstream")
        if not self.weather.has_data or method not in self.weather.values:
            raise ValueNotSet(f"{method}: no cached value yet")
        return self.weather.values[method]

    def status(self) -> dict[str, Any]:
        return {
            "is_safe": self.is_safe(),
            "safety": self._device_status(self.safety),
            "weather": self._device_status(self.weather),
        }

    # -- internals -----------------------------------------------------------

    async def _poll_device(
        self,
        state: DeviceState,
        device: DeviceConfig,
        properties: tuple[str, ...],
    ) -> None:
        # Re-learn "not implemented" after a bad cycle so a device that starts
        # answering a property recovers instead of being wrong forever.
        if state.consecutive_failures:
            state.not_implemented.clear()

        values: dict[str, Any] = {}
        error: str | None = None

        for method in properties:
            if method in state.not_implemented:
                continue
            try:
                values[method] = await self._client.get(
                    device.device_type, device.upstream_device_number, method
                )
            except UpstreamNotImplemented:
                state.not_implemented.add(method)
                logger.info(
                    "%s: %s is not implemented upstream, skipping", device.name, method
                )
            except UpstreamError as exc:
                # Keep polling the rest of the cycle; one error fails the cycle.
                if error is None:
                    error = str(exc)

        if error is None:
            self._record_success(state, device, values)
        else:
            self._record_failure(state, device, error)

    def _record_success(
        self, state: DeviceState, device: DeviceConfig, values: dict[str, Any]
    ) -> None:
        if state.consecutive_failures:
            logger.info(
                "%s: recovered after %d consecutive failed polls",
                device.name,
                state.consecutive_failures,
            )
        state.consecutive_failures = 0
        state.last_success_utc = datetime.now(timezone.utc)
        state.values = values

    def _record_failure(
        self, state: DeviceState, device: DeviceConfig, reason: str
    ) -> None:
        state.consecutive_failures += 1
        logger.warning(
            "%s: poll failed (%d consecutive): %s",
            device.name,
            state.consecutive_failures,
            reason,
        )
        # Log the crossing once, not once per cycle while it stays down.
        if state.consecutive_failures == state.threshold:
            logger.error(
                "%s: %d consecutive failed polls, reporting unsafe",
                device.name,
                state.threshold,
            )

    @staticmethod
    def _device_status(state: DeviceState) -> dict[str, Any]:
        return {
            "consecutive_failures": state.consecutive_failures,
            "failure_threshold": state.threshold,
            "failed": state.failed,
            "last_success_utc": (
                state.last_success_utc.isoformat() if state.last_success_utc else None
            ),
            "values": dict(state.values),
            "not_implemented": sorted(state.not_implemented),
        }
