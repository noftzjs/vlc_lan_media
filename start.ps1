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

# Serve VLC's web interface from our copy of its HTTP folder: it adds requests/tracks.json,
# which the audio/subtitle switcher needs. Forward slashes matter inside the Lua string.
$VlcHttpDir  = (Join-Path $PSScriptRoot "vlc_http") -replace "\\", "/"

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
        "--qt-minimal-view"
    )
    Start-Sleep -Seconds 2
} else {
    Write-Host "VLC is already running. Close it and re-run this script if track switching doesn't work;"
    Write-Host "it needs VLC launched with this script's flags."
}

$ip = (Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias "Ethernet","Wi-Fi" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty IPAddress)
Write-Host ""
Write-Host "LAN Projector Queue is at:  http://${ip}:$WebPort" -ForegroundColor Green
Write-Host "(If other machines can't reach it, allow TCP $WebPort through Windows Firewall; see README.md)"
Write-Host ""

& "$PSScriptRoot\.venv\Scripts\uvicorn.exe" app:app --host 0.0.0.0 --port $WebPort
