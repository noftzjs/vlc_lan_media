# Launches VLC (with its web interface enabled) and the FastAPI server.
# Run from PowerShell:  .\start.ps1
# Then open http://queue.lan from any device on the LAN (the router's DNS record points it at
# this PC), or http://<this-pc's-LAN-IP>. /qr shows both as QR codes for the projector.

$ErrorActionPreference = "Stop"
$VlcExe      = "C:\Program Files\VideoLAN\VLC\vlc.exe"
$VlcPassword = "Password123"
$VlcPort     = 8080
$WebPort     = 80          # so the address needs no :port; falls back to 8000 if 80 is taken
$PublicUrl   = "http://queue.lan"

if (Get-NetTCPConnection -LocalPort $WebPort -State Listen -ErrorAction SilentlyContinue) {
    Write-Warning "Port $WebPort is already in use (IIS, Skype or another web server?); using 8000 instead."
    $WebPort   = 8000
    $PublicUrl = "${PublicUrl}:8000"
}

# Pass the settings to app.py (it also has these as defaults).
$env:VLC_PASSWORD = $VlcPassword
$env:VLC_PORT     = "$VlcPort"
$env:WEB_PORT     = "$WebPort"
$env:PUBLIC_URL   = $PublicUrl
# $env:MEDIA_DIRS = "N:\TV Shows;N:\Movies;N:\Anime"   # uncomment to change which folders are shared

# YouTube support: yt-dlp needs a JavaScript runtime. Fetch a portable Deno once into tools\deno,
# and keep yt-dlp current because YouTube changes often (a quick no-op when already up to date).
$DenoExe = Join-Path $PSScriptRoot "tools\deno\deno.exe"
if (-not (Test-Path $DenoExe)) {
    Write-Host "Downloading Deno (needed for YouTube playback)..."
    $denoDir = Split-Path $DenoExe
    New-Item -ItemType Directory -Force $denoDir | Out-Null
    try {
        Invoke-WebRequest -Uri "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip" -OutFile "$denoDir\deno.zip" -UseBasicParsing
        Expand-Archive "$denoDir\deno.zip" $denoDir -Force
        Remove-Item "$denoDir\deno.zip"
    } catch { Write-Warning "Deno download failed; YouTube won't work until tools\deno\deno.exe exists. $_" }
}
try { & "$PSScriptRoot\.venv\Scripts\python.exe" -m pip install -q -U "yt-dlp[default]" 2>$null | Out-Null } catch { Write-Warning "Could not update yt-dlp (offline?)" }

# Serve VLC's web interface from our copy of its HTTP folder: it adds requests/tracks.json,
# which the audio/subtitle switcher needs. Forward slashes matter inside the Lua string.
$VlcHttpDir  = (Join-Path $PSScriptRoot "vlc_http") -replace "\\", "/"

# A VLC opened by hand (double-clicking a file, Start menu) lacks our flags: no web interface
# folder, so track switching and track memory don't work. Restart it properly in that case.
$running = Get-CimInstance Win32_Process -Filter "Name = 'vlc.exe'" -ErrorAction SilentlyContinue
if ($running -and -not ($running.CommandLine -join ' ').Contains('--lua-config')) {
    Write-Host "VLC is running without this script's settings; restarting it..."
    $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}

# Crash evidence: VLC appends a log to cache\vlc.log (warnings and errors; --log-verbose=3 adds
# debug). A crash leaves a dump in %APPDATA%\vlc\crashdump, and answering VLC's "send a bug report?"
# prompt on the next start deletes it, so it is copied to cache\vlc_crashdumps first.
$VlcLog    = Join-Path $PSScriptRoot "cache\vlc.log"
$CrashDir  = Join-Path $env:APPDATA "vlc\crashdump"
$CrashKeep = Join-Path $PSScriptRoot "cache\vlc_crashdumps"
New-Item -ItemType Directory -Force (Split-Path $VlcLog) | Out-Null

function Save-VlcCrashDumps {
    $dumps = Get-ChildItem $CrashDir -File -ErrorAction SilentlyContinue
    if (-not $dumps) { return }
    New-Item -ItemType Directory -Force $CrashKeep | Out-Null
    foreach ($d in $dumps) {  # named by the dump's own time, so a dump still waiting on the prompt is kept once
        $keep = Join-Path $CrashKeep ("{0:yyyyMMdd-HHmmss}-{1}" -f $d.LastWriteTime, $d.Name)
        if (Test-Path $keep) { continue }
        Copy-Item $d.FullName $keep
        Write-Warning "VLC left a crash dump; copied to $keep (log: $VlcLog)"
    }
}

function Start-Vlc {
    Save-VlcCrashDumps
    Write-Host "Starting VLC with web interface on port $VlcPort..."
    # --http-host 127.0.0.1 keeps VLC's own web UI off the LAN; only this app talks to it.
    Start-Process $VlcExe -ArgumentList @(
        "--extraintf", "http",
        "--http-host", "127.0.0.1",
        "--http-port", "$VlcPort",
        "--http-password", $VlcPassword,
        "--lua-config", "http={dir='$VlcHttpDir'}",
        "--audio-language", "eng,en",     # VLC ignores this for many MKVs; the app's track memory covers it
        "--audio-track=-1", "--audio-track-id=-1",  # vlcrc had audio-track=1 / audio-track-id=2 saved: files started silent
        "--fullscreen",
        "--no-video-title-show",
        "--no-interact",                  # no modal error dialogs: an unplayable item is skipped, not a frozen player
        "--no-random", "--no-loop", "--no-repeat",  # the app's feeder needs VLC to play its list in order
        "--file-logging", "--logfile=`"$VlcLog`"", "--log-verbose=2",  # appends, so a relaunch keeps the crash's lines
        "--qt-minimal-view"
    )
    # Wait until VLC's web interface answers (up to ~15s) so the app doesn't start against nothing.
    $ready = $false
    for ($i = 0; $i -lt 30 -and -not $ready; $i++) {
        Start-Sleep -Milliseconds 500
        $ready = $null -ne (Get-NetTCPConnection -LocalPort $VlcPort -State Listen -ErrorAction SilentlyContinue)
    }
    if ($ready) { Write-Host "VLC web interface is up." } else { Write-Warning "VLC did not open port $VlcPort." }
}

if (-not (Get-Process vlc -ErrorAction SilentlyContinue)) {
    Start-Vlc
} else {
    Write-Host "VLC is already running with this script's settings."
}

$ip = (Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias "Ethernet","Wi-Fi" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty IPAddress)
$portSuffix = if ($WebPort -eq 80) { "" } else { ":$WebPort" }
Write-Host ""
Write-Host "LAN Projector Queue is at:  $PublicUrl  (or http://${ip}$portSuffix)" -ForegroundColor Green
Write-Host "QR codes for the projector: http://localhost$portSuffix/qr"
$rule = Get-NetFirewallRule -DisplayName "LAN Projector Queue" -ErrorAction SilentlyContinue
if (-not $rule -or "$WebPort" -notin @(($rule | Get-NetFirewallPortFilter).LocalPort)) {
    Write-Warning "The 'LAN Projector Queue' firewall rule doesn't allow port $WebPort; phones may not reach the page. See README.md, 'One-time setup'."
}
Write-Host "Stop with Ctrl+C here. Closing VLC on its own just gets it relaunched."
Write-Host ""

# The server runs alongside a watchdog: if VLC dies (a crash, or closed at the projector) it is
# started again with the flags above and the app's feeder carries on (an item cut off part-way
# is held until Play is pressed on the page, like a stop at the projector).
$web = Start-Process "$PSScriptRoot\.venv\Scripts\uvicorn.exe" -ArgumentList "app:app", "--host", "0.0.0.0", "--port", "$WebPort" -NoNewWindow -PassThru
try {
    while (-not $web.HasExited) {
        Start-Sleep -Seconds 5
        if (-not $web.HasExited -and -not (Get-Process vlc -ErrorAction SilentlyContinue)) {
            Write-Warning "VLC is gone; relaunching it ($(Get-Date -Format 'HH:mm:ss'))"
            Start-Vlc
        }
    }
} finally {
    # Ctrl+C reaches uvicorn too; give it time to shut down cleanly, then end it and its python child.
    if (-not $web.WaitForExit(10000)) { taskkill /T /F /PID $web.Id 2>$null | Out-Null }
}
