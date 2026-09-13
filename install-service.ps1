<#
.SYNOPSIS
    Registers (or removes) the AlpacaProxy scheduled task.

.DESCRIPTION
    Creates a scheduled task named "AlpacaProxy" that launches
    `python -m alpaca_proxy --config <ConfigPath>` at system startup, running
    whether or not a user is logged on, and restarts it if it exits.

    A scheduled task is used rather than a real service because it is native --
    nothing extra to install.

    Must be run from an elevated PowerShell prompt.

.PARAMETER InstallPath
    Working directory for the task. Default: C:\AlpacaProxy

.PARAMETER PythonPath
    python.exe to run, normally the venv's. Default: <InstallPath>\.venv\Scripts\python.exe

.PARAMETER ConfigPath
    YAML config file to pass with --config. Default: <InstallPath>\config.yaml

.PARAMETER Account
    Account to run as. Default: SYSTEM. A non-SYSTEM account is registered as a
    service account (no stored password, so it cannot be an ordinary user with
    a password prompt); use SYSTEM unless you have a reason not to.

.PARAMETER TaskName
    Scheduled task name. Default: AlpacaProxy

.PARAMETER Uninstall
    Remove the task instead of creating it.

.EXAMPLE
    .\install-service.ps1 -InstallPath C:\AlpacaProxy

.EXAMPLE
    .\install-service.ps1 -Uninstall
#>

[CmdletBinding()]
param(
    [string] $InstallPath = 'C:\AlpacaProxy',
    [string] $PythonPath,
    [string] $ConfigPath,
    [string] $Account = 'SYSTEM',
    [string] $TaskName = 'AlpacaProxy',
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'

function Assert-Elevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'This script must be run from an elevated PowerShell prompt.'
    }
}

function Show-TaskState {
    param([string] $Name)
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "Task '$Name' is not registered."
        return
    }
    $info = Get-ScheduledTaskInfo -TaskName $Name
    Write-Host ''
    Write-Host "Task:        $($task.TaskName)"
    Write-Host "State:       $($task.State)"
    Write-Host "Run as:      $($task.Principal.UserId) ($($task.Principal.RunLevel))"
    Write-Host "Action:      $($task.Actions[0].Execute) $($task.Actions[0].Arguments)"
    Write-Host "Working dir: $($task.Actions[0].WorkingDirectory)"
    Write-Host "Last run:    $($info.LastRunTime) (result $($info.LastTaskResult))"
}

Assert-Elevated

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    }
    else {
        Write-Host "Scheduled task '$TaskName' was not registered; nothing to do."
    }
    Show-TaskState -Name $TaskName
    return
}

if (-not $PythonPath) { $PythonPath = Join-Path $InstallPath '.venv\Scripts\python.exe' }
if (-not $ConfigPath) { $ConfigPath = Join-Path $InstallPath 'config.yaml' }

if (-not (Test-Path -LiteralPath $InstallPath)) {
    throw "Install path not found: $InstallPath"
}
if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw "Python executable not found: $PythonPath (create the venv and pip install . first)"
}
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Config file not found: $ConfigPath (copy config.example.yaml and edit it)"
}

$arguments = '-m alpaca_proxy --config "{0}"' -f $ConfigPath

$action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument $arguments `
    -WorkingDirectory $InstallPath

$trigger = New-ScheduledTaskTrigger -AtStartup

if ($Account -eq 'SYSTEM') {
    $principal = New-ScheduledTaskPrincipal `
        -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
}
else {
    $principal = New-ScheduledTaskPrincipal `
        -UserId $Account -LogonType ServiceAccount -RunLevel Highest
}

# StartWhenAvailable + no execution time limit: this is a long-running daemon,
# not a job that finishes. RestartCount/Interval cover a crash or an upstream
# condition that somehow takes the process down.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Replacing existing scheduled task '$TaskName'."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description 'Caching ASCOM Alpaca proxy for the observatory safety monitor and weather station.' `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

Write-Host "Registered scheduled task '$TaskName'."

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
Show-TaskState -Name $TaskName

Write-Host ''
Write-Host 'Verify with:  Invoke-RestMethod http://127.0.0.1:11111/status'
