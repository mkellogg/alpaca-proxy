# Alpaca Proxy

A local caching ASCOM Alpaca proxy for the observatory safety monitor and
weather station.

## Install

Run these from an elevated PowerShell prompt.

1. Install Python 3.11 or newer from
   [python.org](https://www.python.org/downloads/windows/), ticking **Add
   python.exe to PATH**. Check it with `python --version`.

2. Create the install directory, copy the source into it, then build the venv:

   ```powershell
   New-Item -ItemType Directory -Force C:\AlpacaProxy
   Set-Location C:\AlpacaProxy
   # copy the source tree here, then:
   python -m venv C:\AlpacaProxy\.venv
   C:\AlpacaProxy\.venv\Scripts\pip.exe install .
   ```

## Configure

```powershell
Copy-Item config.example.yaml C:\AlpacaProxy\config.yaml
notepad C:\AlpacaProxy\config.yaml
```

## Run it as a service

```powershell
C:\AlpacaProxy\install-service.ps1 `
    -InstallPath C:\AlpacaProxy `
    -PythonPath  C:\AlpacaProxy\.venv\Scripts\python.exe `
    -ConfigPath  C:\AlpacaProxy\config.yaml
```

This registers a scheduled task named **AlpacaProxy** that starts at boot and
restarts on failure. `-Uninstall` removes it. Confirm it is up:

```powershell
Get-ScheduledTask -TaskName AlpacaProxy
Invoke-RestMethod http://127.0.0.1:11111/status
```

`GET /status` is a plain JSON dump of the poller's cached state; open it in a
browser whenever something looks wrong.

## Point NINA at it

Create a new Alpaca driver for each device — once for `SafetyMonitor`, then the
whole procedure again for `ObservingConditions`. The steps are identical apart
from the device type.

1. Run `C:\Program Files (x86)\ASCOM\Platform\Tools\DriverConnect64\ASCOM.DriverConnect.exe`
2. Select the device type.
3. Click **Choose** to open the ASCOM device driver window.
4. In the **Alpaca** menu at the top, choose **Create Alpaca Driver**.
5. Name it when prompted — this is what appears in NINA's dropdown, so pick
   something that tells the proxied driver apart from any existing direct one.
6. Accept the Windows administrator prompt.
7. Click **Properties** on the ASCOM mini window and enter the settings below.
8. Click **OK**, then **OK** again on the first window.
9. Click **Connect** to test, then disconnect.
10. In NINA, select the new driver on the relevant equipment page.

| Field | Value |
|---|---|
| HTTP Service Type | `http` |
| Host Name or IP | `127.0.0.1` |
| Alpaca Port | `11111` |
| Remote Device number | `0` |
| Enable rediscovery | unchecked |
| Connect/Disconnect Options | Managed connect / disconnect locally |

The Remote Device number is `0` for **both** drivers — not the number the device
has upstream. The proxy re-exposes each device as number 0 of its type.

Design rationale and behaviour notes live in `docs/plans/`.
