<#
.SYNOPSIS
    Installs or updates Meeting Transcripter for the current Windows user.

.DESCRIPTION
    Press Win+R (or open a terminal), paste this and press Enter:

        powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/masoudelete/Meeting-transcripter/HEAD/install.ps1 | iex"

    The installer:
      - finds Python 3.10-3.14, or installs Python 3.12 for this user,
      - creates a private Python environment in %LOCALAPPDATA%\MeetingTranscripter,
      - installs the app (with NVIDIA GPU support when an NVIDIA GPU is present),
      - asks your name and how meeting notes should be written,
      - downloads the speech model,
      - adds a Start menu shortcut, starts the app at sign-in and starts it now.
    No administrator rights are needed. Run it again to update; your settings are kept.

    To pass options when installing from the web:

        & ([scriptblock]::Create((irm https://raw.githubusercontent.com/masoudelete/Meeting-transcripter/HEAD/install.ps1))) -Summarizer ollama

.PARAMETER Source
    Folder with the app's source code. By default it is downloaded from GitHub.
.PARAMETER Ref
    Branch, tag or commit to download (default: the repository's default branch).
.PARAMETER Name
    Your name as it should appear in transcripts.
.PARAMETER Summarizer
    How notes are written: "claude", "ollama" or "none". For "claude" the API key is
    asked for, or taken from the ANTHROPIC_API_KEY environment variable.
.PARAMETER WhisperModel
    Speech model to use instead of the default (large-v3-turbo), e.g. "small".
.PARAMETER NonInteractive
    Ask nothing; use the parameters and defaults.
.PARAMETER NoStart
    Don't start the app at the end.
.PARAMETER NoAutostart
    Don't start the app at sign-in.
#>
param(
    [string]$Source,
    [string]$Ref = "HEAD",
    [string]$Name,
    [string]$Summarizer,
    [string]$WhisperModel,
    [switch]$NonInteractive,
    [switch]$NoStart,
    [switch]$NoAutostart
)

function Install-MeetingTranscripter {
    param(
        [string]$Source,
        [string]$Ref,
        [string]$Name,
        [string]$Summarizer,
        [string]$WhisperModel,
        [bool]$NonInteractive,
        [bool]$NoStart,
        [bool]$NoAutostart
    )
    $ErrorActionPreference = "Stop"
    $ProgressPreference = "SilentlyContinue"  # Invoke-WebRequest is very slow with a progress bar
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $Repo = "masoudelete/Meeting-transcripter"
    $AppName = "Meeting Transcripter"
    $AppDir = Join-Path $env:LOCALAPPDATA "MeetingTranscripter"
    $Venv = Join-Path $AppDir "venv"
    $VenvPy = Join-Path $Venv "Scripts\python.exe"
    $AppExe = Join-Path $Venv "Scripts\meeting-transcripter.exe"
    $ConfigPath = Join-Path $AppDir "config.json"
    $Icon = Join-Path $AppDir "app.ico"
    $StartMenuLink = Join-Path ([Environment]::GetFolderPath("Programs")) "$AppName.lnk"
    $StartupLink = Join-Path ([Environment]::GetFolderPath("Startup")) "$AppName.lnk"

    if ($Summarizer -and @("claude", "ollama", "none") -notcontains $Summarizer) {
        throw "-Summarizer must be claude, ollama or none."
    }
    if (-not [Environment]::Is64BitOperatingSystem -or [Environment]::OSVersion.Version.Major -lt 10) {
        throw "$AppName needs 64-bit Windows 10 or 11."
    }

    Write-Step "Stopping $AppName if it is running"
    Stop-MeetingTranscripter -AppName $AppName

    Write-Step "Finding Python"
    $python = Find-Python
    if (-not $python) {
        $python = Install-Python
    }
    Write-Host "Using Python $($python.Version) at $($python.Path)"

    $downloaded = $null
    if (-not $Source) {
        Write-Step "Downloading $AppName ($Ref)"
        $downloaded = Join-Path $env:TEMP "meeting-transcripter-$PID"
        $Source = Get-AppSource -Repo $Repo -Ref $Ref -Destination $downloaded
    }
    $Source = (Resolve-Path $Source).Path
    if (-not (Test-Path (Join-Path $Source "pyproject.toml"))) {
        throw "$Source does not contain the app's source (pyproject.toml is missing)."
    }

    Write-Step "Creating the Python environment"
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    if ((Test-Path $VenvPy) -and -not (Get-NativeOutput $VenvPy @("-c", "print('ok')"))) {
        Write-Host "The existing environment is broken; recreating it."
        Remove-Item -Recurse -Force $Venv
    }
    if (-not (Test-Path $VenvPy)) {
        Invoke-Native "Creating the environment" { & $python.Path -m venv $Venv }
    }
    Invoke-Native "Updating pip" { & $VenvPy -m pip install --upgrade pip --disable-pip-version-check --quiet }

    $gpu = Test-NvidiaGpu
    if ($gpu) {
        Write-Step "Installing $AppName with NVIDIA GPU support (about 1.5 GB)"
        $requirement = "$Source[cuda]"
    } else {
        Write-Step "Installing $AppName (about 300 MB)"
        $requirement = $Source
    }
    Invoke-Native "Installing the app" { & $VenvPy -m pip install --upgrade --disable-pip-version-check $requirement }
    # Always replace the app's own code, even when its version number did not change.
    Invoke-Native "Installing the app" { & $VenvPy -m pip install --force-reinstall --no-deps --disable-pip-version-check --quiet $Source }
    $version = Get-NativeOutput $VenvPy @("-c", "import meeting_transcripter; print(meeting_transcripter.__version__)")
    if (-not $version) { throw "The app was installed but does not start. See the messages above." }

    Write-Step "Settings"
    $settings = @{}
    $fresh = -not (Test-Path $ConfigPath)
    if ($fresh) {
        if (-not $Name) {
            $Name = Get-DisplayName
            if (-not $NonInteractive) {
                $answer = Read-Host "Your name, as it should appear in transcripts [$Name]"
                if ($answer.Trim()) { $Name = $answer.Trim() }
            }
        }
        if (-not $Summarizer) {
            $Summarizer = "none"
            if (-not $NonInteractive) { $Summarizer = Read-Summarizer }
        }
    }
    if ($Name) { $settings["me_label"] = $Name }
    if ($WhisperModel) { $settings["model"] = $WhisperModel }
    if ($Summarizer) {
        $settings["summarizer"] = $Summarizer
        if ($Summarizer -eq "claude") {
            $key = $env:ANTHROPIC_API_KEY
            if (-not $key -and -not $NonInteractive) { $key = Read-ApiKey }
            if ($key) { $settings["anthropic_api_key"] = $key }
            elseif (-not (Get-ConfigValue $ConfigPath "anthropic_api_key")) {
                Write-Warning "No Claude API key yet. Add it later: tray icon > Settings > anthropic_api_key."
            }
        }
    }
    if ($settings.Count) {
        $file = Join-Path $AppDir "settings.tmp.json"
        [IO.File]::WriteAllText($file, ($settings | ConvertTo-Json -Compress), (New-Object Text.UTF8Encoding $false))
        try {
            Invoke-Native "Saving settings" { & $VenvPy -m meeting_transcripter --merge-settings $file }
        } finally {
            Remove-Item -Force $file -ErrorAction SilentlyContinue
        }
    } else {
        Write-Host "Keeping your settings in $ConfigPath"
    }
    $Summarizer = Get-ConfigValue $ConfigPath "summarizer"
    if ($Summarizer -eq "ollama") { Initialize-Ollama -ConfigPath $ConfigPath -NonInteractive $NonInteractive }

    Write-Step "Downloading the speech model (about 1.6 GB the first time)"
    Invoke-Native "Downloading the speech model" { & $VenvPy -m meeting_transcripter --download-model }

    Write-Step "Adding shortcuts"
    Invoke-Native "Creating the icon" { & $VenvPy -m meeting_transcripter --icon $Icon }
    Copy-Item -Force (Join-Path $Source "uninstall.ps1") (Join-Path $AppDir "uninstall.ps1")
    New-Shortcut -Path $StartMenuLink -Target $AppExe -WorkDir $AppDir -Icon $Icon
    Register-Uninstaller -AppName $AppName -AppDir $AppDir -Icon $Icon -Version $version

    $autostart = "off"
    if ($NoAutostart) {
        try { (Get-TaskFolder).DeleteTask($AppName, 0) } catch { Write-Verbose "No sign-in task: $_" }
        Remove-Item -Force $StartupLink -ErrorAction SilentlyContinue
    } else {
        $autostart = Register-Autostart -AppName $AppName -AppExe $AppExe -AppDir $AppDir -StartupLink $StartupLink -Icon $Icon
    }

    if (-not $NoStart) {
        Write-Step "Starting $AppName"
        if ($autostart -eq "task") {
            (Get-TaskFolder).GetTask($AppName).Run($null) | Out-Null
        } else {
            Start-Process -FilePath $AppExe -WorkingDirectory $AppDir
        }
    }
    if ($downloaded) { Remove-Item -Recurse -Force $downloaded -ErrorAction SilentlyContinue }

    $meetings = Get-ConfigValue $ConfigPath "meetings_dir"
    if (-not $meetings) { $meetings = Join-Path $env:USERPROFILE "Meetings" }
    $notes = switch ($Summarizer) {
        "claude" { "Claude API" }
        "ollama" { "Ollama (local)" }
        default { "off (transcripts only)" }
    }
    Write-Host ""
    Write-Host "$AppName $version is installed." -ForegroundColor Green
    if (-not $NoStart) {
        Write-Host "It is running: look for the grey dot in the system tray (it may be under the ^ arrow)."
    }
    Write-Host "  Meetings:      $meetings"
    Write-Host "  Transcription: $(if ($gpu) { 'NVIDIA GPU' } else { 'CPU (a call takes about as long as it lasted to transcribe)' })"
    Write-Host "  Notes:         $notes"
    Write-Host "  Settings:      right-click the tray icon > Settings ($ConfigPath)"
    Write-Host "  Starts at sign-in: $(if ($autostart -eq 'off') { 'no' } else { 'yes' })"
    Write-Host "  Uninstall:     Windows Settings > Apps > $AppName"
    Write-Host ""
    Write-Host "Recording a call may require the consent of everyone in it. Tell people when you record." -ForegroundColor Yellow
}

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Invoke-Native {
    # Runs a native command, shows its output, and fails when it fails. Windows PowerShell
    # turns stderr lines into errors under "Stop", so this runs under "Continue".
    param([string]$What, [scriptblock]$Command)
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Command | Out-Host
    } finally {
        $ErrorActionPreference = $old
    }
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)." }
}

function Get-NativeOutput {
    # stdout of a native command, or $null when it is missing or fails.
    param([string]$Exe, [string[]]$Arguments)
    if (-not (Get-Command $Exe -ErrorAction SilentlyContinue)) { return $null }
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $global:LASTEXITCODE = 0
        $out = & $Exe @Arguments 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return (@($out) -join "`n").Trim()
    } catch {
        return $null
    } finally {
        $ErrorActionPreference = $old
    }
}

function Get-TaskService {
    # The Task Scheduler COM API works under any execution policy, unlike the ScheduledTasks module.
    $service = New-Object -ComObject Schedule.Service
    $service.Connect()
    return $service
}

function Get-TaskFolder {
    return (Get-TaskService).GetFolder("\")
}

function Stop-MeetingTranscripter([string]$AppName) {
    try {
        (Get-TaskFolder).GetTask($AppName).Stop(0)
    } catch {
        Write-Verbose "No running sign-in task: $_"
    }
    Stop-AppProcesses -AppDir (Join-Path $env:LOCALAPPDATA "MeetingTranscripter")
}

function Stop-AppProcesses([string]$AppDir) {
    # The app's processes run from its venv, or have the venv's path on their command line.
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe' OR Name='meeting-transcripter.exe'" |
        Where-Object { "$($_.ExecutablePath) $($_.CommandLine)".IndexOf($AppDir, [StringComparison]::OrdinalIgnoreCase) -ge 0 } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
}

function Find-Python {
    # Prefers 3.12, which every dependency ships wheels for. Only lists what "py" knows:
    # asking it for a missing version can start an interactive install.
    $code = "import sys; print('%d.%d|%s' % (sys.version_info[0], sys.version_info[1], sys.executable))"
    $paths = @()
    $listing = Get-NativeOutput "py" @("-0p")
    if ($listing) {
        $paths += [regex]::Matches($listing, "[A-Za-z]:\\[^\r\n*]*?python\.exe") | ForEach-Object { $_.Value.Trim() }
    }
    foreach ($dir in @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles")) {
        foreach ($v in @("312", "313", "311", "314", "310")) { $paths += "$dir\Python$v\python.exe" }
    }
    foreach ($cmd in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
        if ($cmd.Source -notmatch "\\WindowsApps\\") { $paths += $cmd.Source }
    }
    $found = @{}
    foreach ($path in ($paths | Select-Object -Unique)) {
        if (-not (Test-Path $path)) { continue }
        $out = Get-NativeOutput $path @("-c", $code)
        if ($out -match "^(3\.(1[0-4]))\|(.+)$" -and -not $found.ContainsKey($Matches[1])) {
            $found[$Matches[1]] = [pscustomobject]@{ Version = $Matches[1]; Path = $Matches[3].Trim() }
        }
    }
    foreach ($v in @("3.12", "3.13", "3.11", "3.14", "3.10")) {
        if ($found.ContainsKey($v)) { return $found[$v] }
    }
    return $null
}

function Install-Python {
    Write-Host "Python 3.10-3.14 was not found. Installing Python 3.12 for this user."
    if (Get-Command winget.exe -ErrorAction SilentlyContinue) {
        $old = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            winget install --id Python.Python.3.12 --exact --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
        } catch {
            Write-Host "winget could not install Python: $_"
        } finally {
            $ErrorActionPreference = $old
        }
        $python = Find-Python
        if ($python) { return $python }
    }
    $url = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
    $file = Join-Path $env:TEMP "python-3.12.10-amd64.exe"
    Write-Host "Downloading $url"
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $file
    $process = Start-Process -FilePath $file -Wait -PassThru `
        -ArgumentList "/quiet InstallAllUsers=0 Include_launcher=0 Include_test=0 PrependPath=0 Shortcuts=0"
    Remove-Item -Force $file -ErrorAction SilentlyContinue
    if ($process.ExitCode -ne 0) { throw "The Python installer failed (exit code $($process.ExitCode))." }
    $python = Find-Python
    if (-not $python) { throw "Python was installed but cannot be found. Open a new PowerShell window and run the installer again." }
    return $python
}

function Get-AppSource([string]$Repo, [string]$Ref, [string]$Destination) {
    $zip = "$Destination.zip"
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/$Repo/archive/$Ref.zip" -OutFile $zip
    if (Test-Path $Destination) { Remove-Item -Recurse -Force $Destination }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($zip, $Destination)
    Remove-Item -Force $zip
    $root = Get-ChildItem -Path $Destination -Directory | Select-Object -First 1
    if (-not $root) { throw "The download from GitHub was empty." }
    return $root.FullName
}

function Test-NvidiaGpu {
    try {
        return [bool](Get-CimInstance Win32_VideoController | Where-Object { $_.Name -match "NVIDIA" })
    } catch {
        return $false
    }
}

function Get-DisplayName {
    try {
        $account = Get-CimInstance Win32_UserAccount -Filter "LocalAccount=True AND Name='$env:USERNAME'"
        if ($account -and $account.FullName) { return $account.FullName }
    } catch {
        Write-Verbose "No display name: $_"
    }
    return $env:USERNAME
}

function Read-Summarizer {
    Write-Host ""
    Write-Host "After each call, how should meeting notes (summary, decisions, action items) be written?"
    Write-Host "  1) Claude API - best notes. The transcript is sent to Anthropic. Needs an API key (pay per use)."
    Write-Host "  2) Ollama     - a local model. Nothing leaves this computer. Needs Ollama and a fast PC."
    Write-Host "  3) No notes   - transcripts only."
    while ($true) {
        switch ((Read-Host "Choose 1, 2 or 3 [1]").Trim()) {
            "" { return "claude" }
            "1" { return "claude" }
            "2" { return "ollama" }
            "3" { return "none" }
        }
    }
}

function Read-ApiKey {
    Write-Host "Create a Claude API key at https://console.anthropic.com/settings/keys"
    $secure = Read-Host "Paste the API key (Enter to skip)" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim()
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    if ($key -and -not $key.StartsWith("sk-ant-")) {
        Write-Warning "That doesn't look like a Claude API key (they start with sk-ant-). It was saved anyway."
    }
    return $key
}

function Get-ConfigValue([string]$ConfigPath, [string]$Key) {
    if (-not (Test-Path $ConfigPath)) { return $null }
    try {
        $config = [IO.File]::ReadAllText($ConfigPath) | ConvertFrom-Json
        return $config.$Key
    } catch {
        return $null
    }
}

function Initialize-Ollama([string]$ConfigPath, [bool]$NonInteractive) {
    $model = Get-ConfigValue $ConfigPath "ollama_model"
    if (-not $model) { $model = "llama3.1:8b" }
    $ollama = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if (-not $ollama) {
        Write-Warning "Ollama is not installed. Install it from https://ollama.com/download, then run: ollama pull $model"
        return
    }
    if (Get-NativeOutput $ollama.Source @("show", $model)) {
        Write-Host "Ollama has $model."
        return
    }
    if (-not $NonInteractive) {
        $answer = Read-Host "Download the note-writing model $model for Ollama now (about 5 GB)? [Y/n]"
        if ($answer.Trim() -notmatch "^[nN]") {
            Invoke-Native "Downloading $model" { & $ollama.Source pull $model }
            return
        }
    }
    Write-Host "Before your first meeting, run: ollama pull $model"
}

function New-Shortcut([string]$Path, [string]$Target, [string]$WorkDir, [string]$Icon) {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($Path)
    $link.TargetPath = $Target
    $link.WorkingDirectory = $WorkDir
    $link.IconLocation = $Icon
    $link.Description = "Records and transcribes your calls"
    $link.Save()
}

function Register-Autostart([string]$AppName, [string]$AppExe, [string]$AppDir, [string]$StartupLink, [string]$Icon) {
    # A sign-in task restarts the app if it crashes; the Startup folder is the fallback
    # where policy doesn't allow users to create tasks.
    $user = "$env:USERDOMAIN\$env:USERNAME"
    try {
        $service = Get-TaskService
        $folder = $service.GetFolder("\")
        $task = $service.NewTask(0)
        $task.RegistrationInfo.Description = "Starts Meeting Transcripter at sign-in."
        $task.Settings.DisallowStartIfOnBatteries = $false
        $task.Settings.StopIfGoingOnBatteries = $false
        $task.Settings.ExecutionTimeLimit = "PT0S"
        $task.Settings.MultipleInstances = 2  # ignore new
        $task.Settings.RestartCount = 5
        $task.Settings.RestartInterval = "PT1M"
        $task.Principal.UserId = $user
        $task.Principal.LogonType = 3  # interactive token
        $task.Principal.RunLevel = 0  # not elevated
        $trigger = $task.Triggers.Create(9)  # at log on
        $trigger.UserId = $user
        $action = $task.Actions.Create(0)  # run a program
        $action.Path = $AppExe
        $action.WorkingDirectory = $AppDir
        $folder.RegisterTaskDefinition($AppName, $task, 6, $user, $null, 3) | Out-Null  # create or update
        Remove-Item -Force $StartupLink -ErrorAction SilentlyContinue
        return "task"
    } catch {
        Write-Host "Could not create a sign-in task ($($_.Exception.Message)); using the Startup folder instead."
        New-Shortcut -Path $StartupLink -Target $AppExe -WorkDir $AppDir -Icon $Icon
        return "startup"
    }
}

function Register-Uninstaller([string]$AppName, [string]$AppDir, [string]$Icon, [string]$Version) {
    $key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\MeetingTranscripter"
    $uninstall = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$AppDir\uninstall.ps1`""
    New-Item -Path $key -Force | Out-Null
    $values = @{
        DisplayName = $AppName
        DisplayVersion = $Version
        Publisher = "github.com/masoudelete/Meeting-transcripter"
        InstallLocation = $AppDir
        DisplayIcon = $Icon
        UninstallString = $uninstall
        QuietUninstallString = "$uninstall -Quiet"
        URLInfoAbout = "https://github.com/masoudelete/Meeting-transcripter"
    }
    foreach ($name in $values.Keys) {
        New-ItemProperty -Path $key -Name $name -Value $values[$name] -PropertyType String -Force | Out-Null
    }
    foreach ($name in @("NoModify", "NoRepair")) {
        New-ItemProperty -Path $key -Name $name -Value 1 -PropertyType DWord -Force | Out-Null
    }
}

try {
    Install-MeetingTranscripter -Source $Source -Ref $Ref -Name $Name -Summarizer $Summarizer `
        -WhisperModel $WhisperModel -NonInteractive $NonInteractive.IsPresent -NoStart $NoStart.IsPresent `
        -NoAutostart $NoAutostart.IsPresent
} catch {
    Write-Host ""
    Write-Host "Installation failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Run the installer again, or report the problem at https://github.com/masoudelete/Meeting-transcripter/issues"
    if ($NonInteractive) { throw }
} finally {
    # Started from Win+R, the window would close before the result can be read.
    if (-not $NonInteractive) { Read-Host "Press Enter to close" | Out-Null }
}
