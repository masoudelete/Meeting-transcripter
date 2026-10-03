<#
.SYNOPSIS
    End-to-end check of an installed Meeting Transcripter (used by CI).

.DESCRIPTION
    Synthesizes a short "call" with the Windows speech synthesizer, lays it out the way the
    recorder does (meta.json + parts\*.wav), runs --finalize with the installed app and checks
    the transcript. Also smoke-tests the Windows-only call detection code.
#>
$ErrorActionPreference = "Stop"
$AppDir = Join-Path $env:LOCALAPPDATA "MeetingTranscripter"
$Py = Join-Path $AppDir "venv\Scripts\python.exe"

function Assert([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "FAILED: $Message" }
    Write-Host "ok: $Message"
}

Write-Host "--- Windows detection code"
$out = & $Py -c @"
from meeting_transcripter import recorder
print('mic users:', sorted(recorder.mic_users()))
print('windows:', len(recorder.visible_windows()))
print('single instance:', recorder.single_instance())
"@
if ($LASTEXITCODE -ne 0) { throw "FAILED: detection smoke test" }
$out | Write-Host
Assert ((@($out) -join "`n") -match "single instance: True") "single-instance mutex"

Write-Host "--- Transcription"
$meetings = Join-Path $env:RUNNER_TEMP "e2e-meetings"
$folder = Join-Path $meetings "2026-10-02_1400"
New-Item -ItemType Directory -Force -Path (Join-Path $folder "parts") | Out-Null

Add-Type -AssemblyName System.Speech
$voice = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voices = @($voice.GetInstalledVoices() | Where-Object { $_.Enabled })
Assert ($voices.Count -gt 0) "a speech synthesizer voice is installed"
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$voice.SetOutputToWaveFile((Join-Path $folder "parts\001_others.wav"), $format)
$voice.Rate = -1
$voice.Speak("Good morning everyone. This is a test of the meeting recorder. Please send the quarterly report to Isabelle by Thursday.")
$voice.Dispose()

# The microphone track: two seconds of silence, which the recorder leaves out.
$silence = New-Object byte[] (16000 * 2 * 2)
$header = New-Object IO.MemoryStream
$w = New-Object IO.BinaryWriter($header)
$w.Write([Text.Encoding]::ASCII.GetBytes("RIFF")); $w.Write([int](36 + $silence.Length))
$w.Write([Text.Encoding]::ASCII.GetBytes("WAVEfmt ")); $w.Write([int]16); $w.Write([int16]1); $w.Write([int16]1)
$w.Write([int]16000); $w.Write([int](16000 * 2)); $w.Write([int16]2); $w.Write([int16]16)
$w.Write([Text.Encoding]::ASCII.GetBytes("data")); $w.Write([int]$silence.Length); $w.Write($silence)
[IO.File]::WriteAllBytes((Join-Path $folder "parts\002_mic.wav"), $header.ToArray())

$meta = @{
    status = "recorded"; trigger = "manual"; apps = @(); window_titles = @("Quarterly review | Microsoft Teams")
    started_at = "2026-10-02T14:00:00-04:00"; ended_at = "2026-10-02T14:00:20-04:00"
    parts = @(
        @{ kind = "others"; file = "001_others.wav"; offset = 0.0; device = "test speakers" },
        @{ kind = "mic"; file = "002_mic.wav"; offset = 0.0; device = "test mic" }
    )
}
[IO.File]::WriteAllText((Join-Path $folder "meta.json"), ($meta | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding $false))

& $Py -m meeting_transcripter --finalize $folder
if ($LASTEXITCODE -ne 0) { throw "FAILED: --finalize exited with $LASTEXITCODE" }

$done = Join-Path $meetings "2026-10-02_1400_quarterly-review"
Assert (Test-Path $done) "the folder is renamed after the Teams meeting title"
$transcript = [IO.File]::ReadAllText((Join-Path $done "transcript.md"))
Write-Host $transcript
Assert ($transcript -match "# Quarterly review") "the transcript is titled after the meeting"
Assert ($transcript -match "(?i)quarterly report") "the speech was transcribed"
Assert ($transcript -match "(?i)thursday") "the whole sentence was transcribed"
$micLines = @($transcript -split "`n" | Where-Object { $_ -match "^\[\d\d:\d\d:\d\d\] " -and $_ -notmatch "^\[[\d:]+\] Others: " })
Assert ($micLines.Count -eq 0) "the silent microphone track adds nothing"
$result = [IO.File]::ReadAllText((Join-Path $done "meta.json")) | ConvertFrom-Json
Assert ($result.status -eq "transcribed") "meta.json status is transcribed"
Assert ($result.language -eq "en") "the language is detected as English"
Assert (-not (Test-Path (Join-Path $done "parts"))) "the raw parts are cleaned up"
Assert (Test-Path (Join-Path $done "others.opus")) "the call audio is kept as Opus"

Write-Host "--- Notes without a summarizer"
& $Py -m meeting_transcripter --notes $done
$result = [IO.File]::ReadAllText((Join-Path $done "meta.json")) | ConvertFrom-Json
Assert ($result.status -eq "failed" -and $result.error -match "no summarizer set up") "a clear error when notes are not set up"

Write-Host "All end-to-end checks passed." -ForegroundColor Green
