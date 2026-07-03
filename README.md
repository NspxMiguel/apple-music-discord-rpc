# apple-music-discord-rpc

Discord Rich Presence for Apple Music — macOS (original) and **Windows** (port).

Shows the current track with album artwork, progress bar, and "Listening to Apple Music" status. Optionally updates your Discord custom status with synced lyrics.

<img width="230" height="47" alt="macOS screenshot" src="https://github.com/user-attachments/assets/2e168586-4202-46a3-a2d5-0e4e499ecdc6" />
<img width="296" height="128" alt="macOS screenshot" src="https://github.com/user-attachments/assets/d5c01904-d43e-4f10-990d-2c75ff3acc61" />

---

## Windows

### Features

- **"Listening to Apple Music"** header (same style as Spotify)
- **Album artwork** fetched from iTunes
- **Progress bar** with current position and total duration
- **Synced lyrics** in Discord custom status via [lrclib.net](https://lrclib.net) (optional, requires token)
- **Settings GUI** — configure everything via a tray icon menu
- **Starts with Windows** (configurable)
- Presence cleared automatically when paused/stopped

### Requirements

- Windows 10 1903+ (Windows Media Session API)
- [Python 3.11+](https://www.python.org/downloads/) — check **"Add Python to PATH"** during install
- Apple Music for Windows (Microsoft Store)
- Discord desktop app open

### Install

Download **[install-windows.bat](https://github.com/spxmiguel/apple-music-discord-rpc/releases/latest)** from Releases and double-click it.

The installer:
1. Detects Python and installs dependencies
2. Copies the script to `C:\apple-music-rpc\`
3. Adds to Windows startup
4. Launches the app

On first launch the **Settings** window opens automatically. Configure your Discord token there to enable lyrics in status.

### Getting your Discord token (for lyrics in status)

> ⚠️ Never share your token. Treat it like a password.

1. Open Discord in your **browser** (discord.com/app)
2. Press **F12** → **Console** tab
3. Paste the following and press Enter:

```js
(webpackChunkdiscord_app.push([[Math.random()],{},({require:e})=>{Object.values(e.c).forEach(x=>{if(x?.exports?.default?.getToken)console.log(x.exports.default.getToken())})}]),0)
```

4. Copy the token that appears in the console output
5. Paste it in the **Settings → Discord → Token** field

### Uninstall

Delete `C:\apple-music-rpc\` and remove the startup entry:

```
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v AppleMusicRPC /f
```

---

## macOS (original)

Deno + JavaScript for Automation (JXA) Discord Rich Presence client for the macOS Apple Music app (Catalina and later) and legacy iTunes.

Works with local tracks and the Apple Music streaming service.

### Features

- Can start in the background at login
- No status bar icon clutter
- Small and (relatively) easy-to-understand script
- Presence is enabled only when music is actually playing
- Apple Music matching
- Local artwork temporary upload on litterbox.catbox.moe as a fallback

### Getting Started

Follow one of the two methods below to download the script and enable the macOS launch agent so it starts at login.

#### Homebrew (Recommended)

After installing [Homebrew](https://brew.sh), run:

```
brew install nextfire/tap/apple-music-discord-rpc
brew services restart apple-music-discord-rpc
```

These commands add [this tap](https://github.com/NextFire/homebrew-tap) to Homebrew, install the `apple-music-discord-rpc` formula (and Deno), and enable the launch agent, starting it immediately.

The `music-rpc.ts` executable is also placed in your `PATH`.

##### Upgrade

```
brew upgrade apple-music-discord-rpc
brew services restart apple-music-discord-rpc
```

##### Uninstall

```
brew services stop apple-music-discord-rpc
brew remove apple-music-discord-rpc
brew untap nextfire/tap
```

#### Shell Scripts

##### Install

Install Deno (v2+), clone the repository, and run `./scripts/install.sh`:

```
git clone https://github.com/NextFire/apple-music-discord-rpc.git
cd apple-music-discord-rpc/
./scripts/install.sh
```

It copies the [launch agent](/scripts/moe.yuru.music-rpc.plist) into `~/Library/LaunchAgents/` and edits it accordingly.

##### Upgrade

```
cd apple-music-discord-rpc/
git fetch && git reset --hard origin/main
./scripts/install.sh
```

##### Uninstall

```
cd apple-music-discord-rpc/
./scripts/uninstall.sh
cd ../
rm -rf apple-music-discord-rpc/
```
