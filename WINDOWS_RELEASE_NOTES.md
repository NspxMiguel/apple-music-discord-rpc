# Windows Release Notes

## Current Windows Port

Highlights:

- Reads Apple Music playback through Windows Global System Media Transport
  Controls.
- Updates Discord Rich Presence on media session events instead of relying only
  on slow polling.
- Sends track changes immediately.
- Performs one delayed timing refresh after a track change to fix
  duration/progress metadata.
- Fetches album artwork from the iTunes Search API.
- Fetches synced lyrics from LRCLIB in the background.
- Updates Discord custom status with synced lyrics when configured with a
  Discord token.
- Clears Rich Presence and custom status on pause, stop, or app close.
- Uses `pypresence.update(...)` for Discord RPC compatibility.
- Keeps logs in `apple-music-rpc.log`.

Known limitations:

- Discord profile popouts can visually cache a previous activity until reopened.
- Some tracks have no synced lyrics in LRCLIB.
- Discord custom status updates can time out or rate-limit independently from
  Rich Presence.
- Windows may emit the new track title before updated timeline metadata; the
  port compensates with one delayed timing refresh.

Do not publish:

- `config.json`
- `cache.sqlite3`
- `*.log`
- `__pycache__/`
- `build/`
- `dist/`

## Manual Test Checklist

1. Start Discord desktop.
2. Start Apple Music for Windows.
3. Start the RPC:

```powershell
powershell -ExecutionPolicy Bypass -File .\run-fixed-backup.ps1
```

4. Play a track and confirm the log shows:

```text
Connected to Discord RPC.
RPC update sent: ...
Playing: ...
```

5. Change tracks and confirm Rich Presence changes.
6. Confirm progress/duration corrects after the one-time timing refresh.
7. Pause and confirm Rich Presence clears.
8. Resume and confirm Rich Presence returns.
9. Play a track with synced LRCLIB lyrics and confirm custom status updates.
10. Play a track without synced lyrics and confirm the log says
    `No synced lyrics found`.

## Suggested GitHub Release Title

Windows port preview

## Suggested GitHub Release Body

This preview release adds Windows support for Apple Music Discord Rich Presence.

It uses Windows media session APIs to read the current Apple Music track and
updates Discord with title, artist, artwork, progress, and optional synced
lyrics in custom status.

This is not merged upstream because the original maintainer does not currently
have a Windows environment to review Windows-specific code.
