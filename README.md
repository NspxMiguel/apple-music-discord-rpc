# Apple Music Discord RPC for Windows

Windows port of `NextFire/apple-music-discord-rpc`.

Shows the currently playing Apple Music track in Discord Rich Presence with:

- "Listening to Apple Music"
- track title, artist, and album
- playback timestamps and progress bar
- album artwork from the iTunes Search API
- optional synced lyrics in Discord custom status via LRCLIB
- automatic clear on pause, stop, or Apple Music close

This Windows port uses the Windows Global System Media Transport Controls API to
read Apple Music playback state.

## Status

This is a Windows-specific port. The upstream maintainer does not currently
review or merge Windows-specific code because they do not have a Windows
environment.

For the original macOS project, see:

https://github.com/NextFire/apple-music-discord-rpc

## Requirements

- Windows 10 1903 or newer
- Apple Music for Windows from the Microsoft Store
- Discord desktop app
- Python 3.11 or newer

Python packages:

```powershell
python -m pip install pypresence winrt-runtime winrt-Windows.Media.Control winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Storage.Streams
```

## Install

Run:

```powershell
install-windows.bat
```

The installer copies the script to `C:\apple-music-rpc`, installs Python
dependencies, and adds a Windows startup entry.

For local testing from this repository:

```powershell
powershell -ExecutionPolicy Bypass -File .\run-windows.ps1
```

## How It Works

The worker listens for Windows media session events:

- `media_properties_changed`
- `playback_info_changed`
- `current_session_changed`
- `sessions_changed`

When Apple Music changes track, the app sends a Discord RPC update immediately.
It then performs one short delayed refresh to correct the timestamp and duration
after Windows finishes updating timeline metadata.

Artwork and lyrics are fetched in background threads so network calls do not
block track changes.

## Lyrics In Discord Status

Lyrics require a Discord user token because Discord's Rich Presence IPC cannot
set a custom status. The token is used only to PATCH your own custom status.

Security notes:

- never share your Discord token
- do not commit `config.json`
- rotate your token if it has been exposed

If `lyrics_emoji` is empty, the app omits `emoji_name` from the Discord payload.

## Troubleshooting

### Rich Presence does not change instantly

Check that only one RPC process is running:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'music-rpc-windows\.py|apple-music-rpc' } |
  Select-Object ProcessId,Name,CommandLine
```

The active process should point at the script you expect.

### Discord shows the old card

The script may have already sent the update while Discord's profile popout is
visually cached. Close and reopen the profile popout. The log file shows what
the script actually sent.

### Progress bar or duration is wrong after changing tracks

Windows sometimes emits the new title before it emits the corrected timeline.
The worker sends an immediate update, then a delayed one-time timing refresh.

### No lyrics

Some tracks do not have synced lyrics in LRCLIB. The log will say:

```text
No synced lyrics found for ...
```

## Logs

Runtime log:

```text
apple-music-rpc.log
```

Useful lines:

```text
RPC update sent: Title|Artist|Album
Playing: Title - Artist
Lyrics loaded: 42 lines for Title|Artist|Album
No synced lyrics found for Title|Artist|Album
Discord status HTTP 401
```

## Uninstall

Remove the startup entry and delete the install folder:

```powershell
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v AppleMusicRPC /f
Remove-Item -Recurse -Force C:\apple-music-rpc
```

Also check the Startup folder if an older installer created a shortcut there:

```powershell
explorer shell:startup
```

## Development Notes

Keep runtime files out of commits:

- `config.json`
- `cache.sqlite3`
- `*.log`
- `__pycache__/`
- `build/`
- `dist/`

The Windows port is intentionally independent from the upstream macOS
implementation because it relies on Windows media session APIs.
