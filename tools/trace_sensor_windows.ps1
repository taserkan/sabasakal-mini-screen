$ErrorActionPreference = "Stop"

Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;

public static class SabasakalWindowTrace
{
    public delegate bool EnumProc(IntPtr handle, IntPtr parameter);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumProc callback, IntPtr parameter);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr handle);

    [DllImport("user32.dll")]
    public static extern int GetWindowText(IntPtr handle, StringBuilder text, int capacity);

    [DllImport("user32.dll")]
    public static extern int GetClassName(IntPtr handle, StringBuilder name, int capacity);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr handle, out uint processId);
}
'@

function Get-VisibleWindowSnapshot {
    $items = [System.Collections.Generic.List[object]]::new()
    $callback = [SabasakalWindowTrace+EnumProc]{
        param([IntPtr]$handle, [IntPtr]$parameter)
        if ([SabasakalWindowTrace]::IsWindowVisible($handle)) {
            $title = [Text.StringBuilder]::new(512)
            $className = [Text.StringBuilder]::new(256)
            [void][SabasakalWindowTrace]::GetWindowText($handle, $title, $title.Capacity)
            [void][SabasakalWindowTrace]::GetClassName($handle, $className, $className.Capacity)
            [uint32]$processId = 0
            [void][SabasakalWindowTrace]::GetWindowThreadProcessId($handle, [ref]$processId)
            $items.Add([pscustomobject]@{
                Handle = $handle.ToInt64()
                Pid = $processId
                Class = $className.ToString()
                Title = $title.ToString()
            })
        }
        return $true
    }
    [void][SabasakalWindowTrace]::EnumWindows($callback, [IntPtr]::Zero)
    return $items
}

$baseline = @(Get-VisibleWindowSnapshot)
$baselineHandles = @($baseline.Handle)
$seen = @{}
$baselineProcessIds = @(Get-Process | Select-Object -ExpandProperty Id)
$seenProcesses = @{}

try {
    $deadline = (Get-Date).AddSeconds(8)
    while ((Get-Date) -lt $deadline) {
        foreach ($window in @(Get-VisibleWindowSnapshot)) {
            if ($window.Handle -notin $baselineHandles -and -not $seen.ContainsKey($window.Handle)) {
                $seen[$window.Handle] = [pscustomobject]@{
                    At = (Get-Date).ToString("HH:mm:ss.fff")
                    Handle = $window.Handle
                    Pid = $window.Pid
                    Class = $window.Class
                    Title = $window.Title
                }
            }
        }
        foreach ($process in @(Get-Process -ErrorAction SilentlyContinue)) {
            if ($process.Id -notin $baselineProcessIds -and -not $seenProcesses.ContainsKey($process.Id)) {
                $seenProcesses[$process.Id] = [pscustomobject]@{
                    At = (Get-Date).ToString("HH:mm:ss.fff")
                    Name = $process.ProcessName
                    Pid = $process.Id
                    ParentPid = $process.Parent.Id
                    Path = $process.Path
                    MainWindowHandle = $process.MainWindowHandle.ToInt64()
                }
            }
        }
        Start-Sleep -Milliseconds 20
    }

    "--- New processes ---"
    @($seenProcesses.Values) | Format-Table -AutoSize
    "--- New visible windows ---"
    @($seen.Values) | Format-Table -AutoSize

    $sensorData = Get-Content -LiteralPath "$env:APPDATA\CS2Screen\elevated-sensors.json" -Raw | ConvertFrom-Json
    $sensorAge = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000 - [double]$sensorData.timestamp
    [pscustomobject]@{
        SensorHostProcesses = @(Get-Process -Name "SabasakalSensorHost" -ErrorAction SilentlyContinue).Count
        SensorAgeSeconds = [math]::Round($sensorAge, 2)
        NewConsoleWindows = @($seen.Values | Where-Object { $_.Class -eq "ConsoleWindowClass" }).Count
        NewVisibleWindows = @($seen.Values).Count
        LaunchMode = "Direct child process"
    } | Format-List
}
finally {
}
