@echo off
:: Extract embedded PowerShell installer and run it
set AMRPC_BAT=%~f0
set TMPPS=%TEMP%\amrpc_install.ps1
powershell -Command "$l=Get-Content '%~f0' -Encoding UTF8;$s=($l|Select-String '^::PS1START$').LineNumber;$e=($l|Select-String '^::PS1END$').LineNumber;$l[$s..($e-2)]|Set-Content '%TMPPS%' -Encoding UTF8"
powershell -ExecutionPolicy Bypass -File "%TMPPS%"
del "%TMPPS%" 2>nul
exit /b

::PS1START
$dest = "C:\apple-music-rpc"
$batFile = "$PSScriptRoot\..\amrpc_install_src.bat"

Write-Host ""
Write-Host " Apple Music Discord RPC - Windows Installer" -ForegroundColor Cyan
Write-Host " =============================================" -ForegroundColor Cyan
Write-Host ""

# Find Python
$candidates = @(
    "$env:LOCALAPPDATA\Programs\Python\Python314\pythonw.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python313\pythonw.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\pythonw.exe",
    "$env:ProgramFiles\Python314\pythonw.exe",
    "$env:ProgramFiles\Python313\pythonw.exe",
    "$env:ProgramFiles\Python312\pythonw.exe",
    "$env:ProgramFiles\Python311\pythonw.exe"
)
$pythonw = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $pythonw) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $pythonw = $cmd.Source }
}
if (-not $pythonw) {
    Write-Host " [ERRO] Python nao encontrado!" -ForegroundColor Red
    Write-Host ""
    Write-Host " Instale Python 3.11+ em: https://www.python.org/downloads/"
    Write-Host " Marque 'Add Python to PATH' durante a instalacao."
    Write-Host ""
    Read-Host "Pressione Enter para sair"
    exit 1
}
Write-Host " [OK] Python: $pythonw" -ForegroundColor Green

# Create destination
if (-not (Test-Path $dest)) { New-Item -ItemType Directory $dest | Out-Null }

# Extract embedded Python script from the .bat file
$batPath = (Get-Item "$env:TEMP\amrpc_install.ps1").Directory.FullName
# Find the original bat by looking for ::PYSTART marker
$srcBat = Get-ChildItem "$env:TEMP" -Filter "amrpc_install*.bat" -ErrorAction SilentlyContinue | Select-Object -First 1
# Use the bat path passed via environment
$origBat = $env:AMRPC_BAT
if (-not $origBat -or -not (Test-Path $origBat)) {
    Write-Host " [ERRO] Nao foi possivel localizar o .bat original." -ForegroundColor Red
    Read-Host "Pressione Enter para sair"
    exit 1
}
$lines = Get-Content $origBat -Encoding UTF8
$s = ($lines | Select-String "^::PYSTART$").LineNumber
$e = ($lines | Select-String "^::PYEND$").LineNumber
$lines[$s..($e-2)] | Set-Content "$dest\music-rpc-windows.py" -Encoding UTF8
Write-Host " [OK] Script extraido para $dest" -ForegroundColor Green

# Install dependencies
Write-Host ""
Write-Host " Instalando dependencias Python (aguarde)..." -ForegroundColor Yellow
$pip = $pythonw -replace "pythonw.exe","python.exe"
& $pip -m pip install --quiet pypresence winrt-runtime "winrt-Windows.Media.Control" "winrt-Windows.Foundation" "winrt-Windows.Foundation.Collections" "winrt-Windows.Storage.Streams" 2>&1 | Out-Null
Write-Host " [OK] Dependencias instaladas." -ForegroundColor Green

# Create VBS launcher
$vbs = "$dest\start.vbs"
@"
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """$pythonw"" ""$dest\music-rpc-windows.py""", 0, False
"@ | Set-Content $vbs -Encoding ASCII

# Add to Startup
$startup = [Environment]::GetFolderPath("Startup")
$wsh = New-Object -ComObject WScript.Shell
$lnk = $wsh.CreateShortcut("$startup\Apple Music RPC.lnk")
$lnk.TargetPath = $vbs
$lnk.Description = "Apple Music Discord Rich Presence"
$lnk.Save()
Write-Host " [OK] Adicionado ao Startup do Windows." -ForegroundColor Green

Write-Host ""
Write-Host " =============================================" -ForegroundColor Cyan
Write-Host " Instalacao concluida!" -ForegroundColor Green
Write-Host " O app vai iniciar agora e com o Windows." -ForegroundColor Green
Write-Host " =============================================" -ForegroundColor Cyan
Write-Host ""

Start-Process wscript.exe $vbs
Write-Host " [OK] Apple Music RPC iniciado em background." -ForegroundColor Green
Write-Host ""
Read-Host "Pressione Enter para fechar"
::PS1END

::PYSTART
#!/usr/bin/env python3
"""
Apple Music Discord Rich Presence - Windows port
Based on https://github.com/NextFire/apple-music-discord-rpc
"""

import asyncio
import json
import time
import sys
import logging
import urllib.request
import urllib.parse
import sqlite3
import os
from pypresence import Presence, InvalidPipe
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

CLIENT_ID     = "773825528921849856"
POLL_INTERVAL = 5
CACHE_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.sqlite3")

def _init_db():
    con = sqlite3.connect(CACHE_FILE)
    con.execute("CREATE TABLE IF NOT EXISTS extras (id TEXT PRIMARY KEY, data TEXT, expires_at INTEGER)")
    con.commit()
    return con

_db = _init_db()

def _cache_get(pid):
    row = _db.execute("SELECT data, expires_at FROM extras WHERE id=?", (pid,)).fetchone()
    if not row:
        return None
    data, expires_at = row
    if expires_at and expires_at < int(time.time() * 1000):
        return None
    return json.loads(data)

def _cache_set(pid, extras):
    _db.execute("INSERT OR REPLACE INTO extras(id, data, expires_at) VALUES(?,?,?)",
                (pid, json.dumps(extras), extras.get("expiresAt")))
    _db.commit()

def _itunes_search(name, artist, album):
    params = urllib.parse.urlencode({"media":"music","entity":"song",
                                     "term":f"{name} {artist} {album}","country":"US"})
    try:
        with urllib.request.urlopen(f"https://itunes.apple.com/search?{params}", timeout=8) as r:
            return json.loads(r.read()).get("results", [])
    except Exception as e:
        log.debug("iTunes error: %s", e)
        return []

def _find_result(results, name, artist, album):
    if not results:
        return None
    nl, al, cll = name.lower(), artist.lower(), album.lower()
    exact = next((r for r in results
                  if cll in r.get("collectionName","").lower()
                  and nl  in r.get("trackName","").lower()
                  and al  in r.get("artistName","").lower()), None)
    if exact:
        return exact
    return next((r for r in results
                 if nl in r.get("trackName","").lower()
                 and al in r.get("artistName","").lower()), None)

def fetch_extras(pid, name, artist, album):
    cached = _cache_get(pid)
    if cached:
        return cached
    results = _itunes_search(name, artist, album)
    r = _find_result(results, name, artist, album)
    if not r and "(" in album:
        clean = album[:album.rfind("(")].strip()
        results = _itunes_search(name, artist, clean)
        r = _find_result(results, name, artist, clean)
    extras = {}
    if r:
        extras["artworkUrl"]        = r.get("artworkUrl100","").replace("100x100bb","600x600bb")
        extras["artistViewUrl"]     = r.get("artistViewUrl")
        extras["collectionViewUrl"] = r.get("collectionViewUrl")
        extras["trackViewUrl"]      = r.get("trackViewUrl")
    _cache_set(pid, extras)
    return extras

def _is_apple_music(source):
    return any(k in source.lower() for k in ["applemusic","apple music","itunes"])

async def _get_track():
    try:
        sessions = await MediaManager.request_async()
    except Exception as e:
        log.debug("MediaManager: %s", e)
        return None
    candidates = []
    cur = sessions.get_current_session()
    if cur:
        candidates.append(cur)
    for s in sessions.get_sessions():
        if s not in candidates:
            candidates.append(s)
    for session in candidates:
        source = session.source_app_user_model_id or ""
        if not _is_apple_music(source):
            continue
        pb = session.get_playback_info()
        if not pb:
            continue
        status = pb.playback_status
        try:
            props = await session.try_get_media_properties_async()
        except Exception:
            continue
        title  = (props.title or "").strip()
        artist = (props.artist or "").strip()
        album  = (props.album_title or "").strip()
        if not title:
            continue
        if not album and " — " in artist:
            artist, album = artist.split(" — ", 1)
            artist = artist.strip(); album = album.strip()
        return {
            "persistent_id": f"{title}|{artist}|{album}",
            "title": title, "artist": artist, "album": album,
            "playing": status == PlaybackStatus.PLAYING,
            "paused":  status == PlaybackStatus.PAUSED,
        }
    return None

def _make_activity(track):
    extras = fetch_extras(track["persistent_id"], track["title"], track["artist"], track["album"])
    kwargs = {
        "details": track["title"][:128],
        "state":   (track["artist"] or "Unknown Artist")[:128],
        "start":   int(time.time()),
    }
    artwork = extras.get("artworkUrl")
    if artwork:
        kwargs["large_image"] = artwork
        kwargs["large_text"]  = (track["album"] or "Apple Music")[:128]
    buttons = []
    if extras.get("trackViewUrl"):
        buttons.append({"label": "Open in Apple Music", "url": extras["trackViewUrl"]})
    sq = urllib.parse.quote(f'artist:{track["artist"]} track:{track["title"]}')
    su = f"https://open.spotify.com/search/{sq}?si"
    if len(su) <= 512:
        buttons.append({"label": "Search on Spotify", "url": su})
    if buttons:
        kwargs["buttons"] = buttons[:2]
    return kwargs

def main():
    log.info("Apple Music Rich Presence (Windows) starting...")
    rpc = None
    last_id = None
    while True:
        if rpc is None:
            try:
                rpc = Presence(CLIENT_ID)
                rpc.connect()
                log.info("Connected to Discord RPC.")
            except InvalidPipe:
                log.warning("Discord not running, retrying in %ds...", POLL_INTERVAL)
                time.sleep(POLL_INTERVAL)
                continue
            except Exception as e:
                log.error("RPC connect error: %s", e)
                rpc = None
                time.sleep(POLL_INTERVAL)
                continue
        try:
            track = asyncio.run(_get_track())
        except Exception as e:
            log.debug("Track fetch error: %s", e)
            track = None
        try:
            if not track or not track["playing"]:
                if last_id is not None:
                    rpc.clear()
                    log.info("Cleared (%s).", "paused" if (track and track["paused"]) else "stopped")
                    last_id = None
            else:
                pid = track["persistent_id"]
                if pid != last_id:
                    rpc.update(**_make_activity(track))
                    log.info("Playing: %s - %s", track["title"], track["artist"] or "Unknown")
                    last_id = pid
        except InvalidPipe:
            log.warning("Lost Discord connection, reconnecting...")
            rpc = None; last_id = None
        except Exception as e:
            log.error("Presence update error: %s", e)
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped.")
        sys.exit(0)
::PYEND
