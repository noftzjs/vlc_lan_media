# Launches VLC (with its web interface enabled) and the FastAPI server.
# Run from PowerShell:  .\start.ps1
# Then open http://<this-pc's-LAN-IP>:8000 from any device on the LAN.

$ErrorActionPreference = "Stop"
$VlcExe      = "C:\Program Files\VideoLAN\VLC\vlc.exe"
$VlcPassword = "Password123"
$VlcPort     = 8080
$WebPort     = 8000

# Pass the settings to app.py (it also has these as defaults).
$env:VLC_PASSWORD = $VlcPassword
$env:VLC_PORT     = "$VlcPort"
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

if (-not (Get-Process vlc -ErrorAction SilentlyContinue)) {
    Write-Host "Starting VLC with web interface on port $VlcPort..."
    # --http-host 127.0.0.1 keeps VLC's own web UI off the LAN; only this app talks to it.
    Start-Process $VlcExe -ArgumentList @(
        "--extraintf", "http",
        "--http-host", "127.0.0.1",
        "--http-port", "$VlcPort",
        "--http-password", $VlcPassword,
        "--lua-config", "http={dir='$VlcHttpDir'}",
        "--audio-language", "eng,en",     # VLC ignores this for many MKVs; the app's track memory covers it
        "--fullscreen",
        "--no-video-title-show",
        "--no-interact",                  # no modal error dialogs: an unplayable item is skipped, not a frozen player
        "--qt-minimal-view"
    )
    # Wait until VLC's web interface answers (up to ~15s) so the app doesn't start against nothing.
    $ready = $false
    for ($i = 0; $i -lt 30 -and -not $ready; $i++) {
        Start-Sleep -Milliseconds 500
        $ready = $null -ne (Get-NetTCPConnection -LocalPort $VlcPort -State Listen -ErrorAction SilentlyContinue)
    }
    if ($ready) { Write-Host "VLC web interface is up." } else { Write-Warning "VLC did not open port $VlcPort." }
} else {
    Write-Host "VLC is already running with this script's settings."
}

$ip = (Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias "Ethernet","Wi-Fi" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty IPAddress)
Write-Host ""
Write-Host "LAN Projector Queue is at:  http://${ip}:$WebPort" -ForegroundColor Green
Write-Host "(If other machines can't reach it, allow TCP $WebPort through Windows Firewall; see README.md)"
Write-Host ""

& "$PSScriptRoot\.venv\Scripts\uvicorn.exe" app:app --host 0.0.0.0 --port $WebPort
