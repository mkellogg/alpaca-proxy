# Alpaca Proxy — caching proxy for the observatory safety monitor and weather station

## Problem

NINA connects directly to the observatory's Alpaca API for two devices:

- `safetymonitor/<building>` — the safety monitor for your building
- `observingconditions/1` — the observatory weather station

Lately these throw transient HTTP errors. A failed `IsSafe` read surfaces in NINA as a
safety fault, which can abort a night for what is really a momentary network blip.

## Goal

A small Python service that sits between NINA and the upstream API. It polls upstream on
its own timer and serves NINA from cache, so NINA's reads never touch the network and
never see a transport error. Real, sustained upstream failure is reported as *unsafe* —
never as an HTTP error.

## Deployment

Runs on the same machine as NINA, bound to `127.0.0.1`, so there is no LAN hop between
NINA and the proxy. Installed as a scheduled task that starts at boot and restarts on
failure.

## Upstream facts (verified 2026-09-12)

Base URL is the observatory's Alpaca API (port 443, https), per its published
ASCOM Alpaca safety and weather guides.

```
management/v1/description   -> ServerName, Manufacturer and Location name the
                               observatory; ManufacturerVersion "0.1.0"
safetymonitor/<building>    -> Name/Description are the building's name,
                               DriverInfo "Safety Monitor Driver",
                               DriverVersion "0.5.0", InterfaceVersion 3, SupportedActions []
observingconditions/1       -> Name/Description are the weather station's name,
                               DriverInfo "Observing Conditions Driver",
                               DriverVersion "0.5.0", InterfaceVersion 2
```

Safety monitor device number is the building number. Weather is always device 1.

Weather properties that return `ErrorNumber 1024 "not implemented"` (HTTP 200):
`cloudcover`, `skyquality`, `starfwhm`. All others return values.

## Behaviour

- Poll every **10s** (configurable).
- After **5** consecutive failed polls (configurable) the safety monitor reports
  `IsSafe = false`.
- Not latching — the first successful poll clears it and `IsSafe` returns to the cached
  upstream value.
- Weather always serves its last good values and never returns an error, except before
  the very first successful poll (`ValueNotSet`, ErrorNumber 1026).
- A weather poll failure past the threshold also drives `IsSafe = false`, because both
  devices sit behind the same upstream host. Config flag `unsafe_on_weather_failure`
  turns that off.
- `1024 not implemented` is passed through verbatim so NINA sees the same driver shape it
  sees today, and is **not** counted as a poll failure.
- Every response is HTTP 200 with an Alpaca error envelope. The proxy never emits 5xx.

## Non-goals

- No UDP discovery responder. Host and port are entered manually in NINA, matching the
  observatory's guide ("Enable rediscovery: unchecked").
- No write-through. Neither device has settable properties worth proxying.
- Not a general Alpaca proxy — two device types, hardcoded method tables.
