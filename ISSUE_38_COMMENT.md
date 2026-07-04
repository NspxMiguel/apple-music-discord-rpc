# Comment For Upstream Windows Support Issue

Hi! I made a Windows port of this project for Apple Music for Windows.

It uses Windows Global System Media Transport Controls to read the current Apple Music session and updates Discord Rich Presence with:

- current track title, artist, and album
- "Listening to Apple Music"
- album artwork from the iTunes Search API
- progress timestamps
- automatic clear on pause/stop
- optional synced lyrics in Discord custom status via LRCLIB
- a small settings GUI/tray workflow

Repository / release:

TODO: add your fork or release URL here

Notes:

- The Windows port is separate from the macOS implementation because it depends on Windows media session APIs.
- Runtime config, cache, and logs are ignored so Discord tokens are not committed.
- The app sends track changes immediately, then performs one short timing refresh because Windows sometimes emits the new title before the corrected duration/timeline metadata.

Thanks for keeping this project alive. Since Windows-specific code will not be merged upstream right now, I am sharing this here for anyone looking for a Windows version.
