<#
.SYNOPSIS
    Removes Meeting Transcripter for the current Windows user.

.DESCRIPTION
    Stops the app and removes its sign-in task, shortcuts, Python environment, settings and log.
    Your meetings (transcripts, notes, audio) are kept unless you pass -RemoveMeetings.

.PARAMETER RemoveMeetings
    Also delete the meetings folder.
.PARAMETER RemoveSpeechModel
    Also delete the downloaded speech models (in %USERPROFILE%\.cache\huggingface).
.PARAMETER Quiet
    Don't wait for Enter at the end.
#>
param(
    [switch]$RemoveMeetings,
    [switch]$RemoveSpeechModel,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$AppName = "Meeting Transcripter"
$AppDir = Join-Path $env:LOCALAPPDATA "MeetingTranscripter"
Set-Location $env:USERPROFILE  # Windows can't delete the current directory

$meetings = Join-Path $env:USERPROFILE "Meetings"
$configPath = Join-Path $AppDir "config.json"
if (Test-Path $configPath) {
    try {
        $configured = ([IO.File]::ReadAllText($configPath) | ConvertFrom-Json).meetings_dir
        if ($configured) { $meetings = $configured }
    } catch {
        Write-Verbose "config.json unreadable: $_"
    }
}

Write-Host "Removing $AppName..."
try {
    $service = New-Object -ComObject Schedule.Service
    $service.Connect()
    $folder = $service.GetFolder("\")
    try { $folder.GetTask($AppName).Stop(0) } catch { Write-Verbose "Task not running: $_" }
    try { $folder.DeleteTask($AppName, 0) } catch { Write-Verbose "No sign-in task: $_" }
} catch {
    Write-Warning "Could not reach the Task Scheduler: $($_.Exception.Message)"
}
# The app's processes run from its venv, or have the venv's path on their command line.
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe' OR Name='meeting-transcripter.exe'" |
    Where-Object { "$($_.ExecutablePath) $($_.CommandLine)".IndexOf($AppDir, [StringComparison]::OrdinalIgnoreCase) -ge 0 } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

foreach ($link in @(
        (Join-Path ([Environment]::GetFolderPath("Programs")) "$AppName.lnk"),
        (Join-Path ([Environment]::GetFolderPath("Startup")) "$AppName.lnk"))) {
    Remove-Item -Force $link -ErrorAction SilentlyContinue
}
Remove-Item -Recurse -Force "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\MeetingTranscripter" -ErrorAction SilentlyContinue

# Files can stay locked for a moment after the app stops.
for ($i = 1; (Test-Path $AppDir) -and $i -le 5; $i++) {
    Start-Sleep -Seconds 1
    Remove-Item -Recurse -Force $AppDir -ErrorAction SilentlyContinue
}
if (Test-Path $AppDir) { Write-Warning "Could not delete everything in $AppDir; delete it by hand after a restart." }

if ($RemoveSpeechModel) {
    Get-ChildItem (Join-Path $env:USERPROFILE ".cache\huggingface\hub") -Directory -Filter "models--*faster-whisper*" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force
}
if ($RemoveMeetings -and (Test-Path $meetings)) {
    Remove-Item -Recurse -Force $meetings
    Write-Host "Deleted your meetings in $meetings."
} elseif (Test-Path $meetings) {
    Write-Host "Your meetings are still in $meetings."
}

Write-Host "$AppName is uninstalled." -ForegroundColor Green
if (-not $RemoveSpeechModel) {
    Write-Host "The speech model is still cached in $env:USERPROFILE\.cache\huggingface (run with -RemoveSpeechModel to delete it)."
}
if (-not $Quiet) { Read-Host "Press Enter to close" | Out-Null }
