#!/usr/bin/env python3
"""
Apple Music Discord Rich Presence - Windows port
Based on https://github.com/NextFire/apple-music-discord-rpc
"""

import asyncio
import json
import math
import time
import sys
import logging
import urllib.request
import urllib.parse
import urllib.error
import sqlite3
import os
import re
import threading
import queue
import winreg
import ctypes
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

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

CLIENT_ID       = "773825528921849856"
VERSION         = "1.4"
DEFAULT_TIMEOUT = 15
MAX_RUNTIME     = 24 * 3600
FAST_CHECK_INTERVAL = 0.25
INACTIVE_CHECK_INTERVAL = 2.0
EVENT_FALLBACK_INTERVAL = 5.0
LYRIC_DISPLAY_LEAD = 0.8  # seconds to send update early to compensate Discord display latency
STATUS_MIN_INTERVAL = 1.5
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE     = os.path.join(BASE_DIR, "config.json")
CACHE_FILE      = os.path.join(BASE_DIR, "cache.sqlite3")
LOG_FILE        = os.path.join(BASE_DIR, "apple-music-rpc.log")

_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
))
logging.getLogger().addHandler(_file_handler)

TOKEN_SCRIPT = (
    "(webpackChunkdiscord_app.push([[Math.random()],{},"
    "(e)=>{e&&e.c&&Object.values(e.c).forEach(x=>{if"
    "(x?.exports?.default?.getToken)"
    "console.log(x.exports.default.getToken())})}]),0)"
)

ITUNES_COUNTRIES = [
    ("US", "United States"),
    ("JP", "Japan"),
    ("GB", "United Kingdom"),
    ("AU", "Australia"),
    ("CA", "Canada"),
    ("BR", "Brazil"),
    ("DE", "Germany"),
    ("FR", "France"),
    ("KR", "South Korea"),
    ("MX", "Mexico"),
    ("IN", "India"),
]

DEFAULT_CONFIG = {
    "discord_token": "",
    "rpc_enabled": True,
    "lyrics_in_status": True,
    "lyrics_in_rpc": True,
    "lyrics_emoji": "\U0001f3b5",
    "hide_game_activity": False,
    "poll_interval": DEFAULT_TIMEOUT,
    "start_with_windows": True,
    "itunes_country": "US",
}

# ── Helpers ────────────────────────────────────────────────────────────────────
def _ensure_len(value: str, min_len: int = 2, max_len: int = 128) -> str:
    if len(value) < min_len:
        return value.ljust(min_len)
    if len(value) > max_len:
        return value[:max_len - 3] + "..."
    return value

# ── Config ─────────────────────────────────────────────────────────────────────
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

# ── SQLite cache ───────────────────────────────────────────────────────────────
def _init_db():
    con = sqlite3.connect(CACHE_FILE, check_same_thread=False)
    con.execute("CREATE TABLE IF NOT EXISTS extras (id TEXT PRIMARY KEY, data TEXT, expires_at INTEGER)")
    con.execute("CREATE TABLE IF NOT EXISTS lyrics  (id TEXT PRIMARY KEY, data TEXT)")
    con.commit()
    return con

_db = _init_db()

def _extras_get(key: str):
    row = _db.execute("SELECT data, expires_at FROM extras WHERE id=?", (key,)).fetchone()
    if not row:
        return None
    data, expires_at = row
    if expires_at and expires_at < int(time.time() * 1000):
        return None
    return json.loads(data)

def _extras_set(key: str, extras: dict):
    _db.execute("INSERT OR REPLACE INTO extras(id,data,expires_at) VALUES(?,?,?)",
                (key, json.dumps(extras), extras.get("expiresAt")))
    _db.commit()

def _lyrics_get(pid: str):
    row = _db.execute("SELECT data FROM lyrics WHERE id=?", (pid,)).fetchone()
    return json.loads(row[0]) if row else None

def _lyrics_set(pid: str, data: dict):
    _db.execute("INSERT OR REPLACE INTO lyrics(id,data) VALUES(?,?)",
                (pid, json.dumps(data)))
    _db.commit()

# ── iTunes Search ──────────────────────────────────────────────────────────────
def _itunes_search(name: str, artist: str, album: str, country: str = "US") -> list:
    params = urllib.parse.urlencode({
        "media": "music", "entity": "song",
        "term": f"{name} {artist} {album}", "country": country,
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(
                f"https://itunes.apple.com/search?{params}", timeout=8
            ) as r:
                return json.loads(r.read()).get("results", [])
        except Exception as e:
            log.debug("iTunes attempt %d: %s", attempt + 1, e)
            time.sleep(0.2)
    return []

def _find_result(results: list, name: str, album: str):
    if not results:
        return None
    if len(results) == 1:
        return results[0]
    nl  = name.lower()
    cll = album.lower()
    return next(
        (r for r in results
         if cll in r.get("collectionName", "").lower()
         and nl  in r.get("trackName", "").lower()),
        None,
    )

def fetch_extras(pid: str, name: str, artist: str, album: str, country: str = "US") -> dict:
    cache_key = f"{pid}|{country}"
    cached = _extras_get(cache_key)
    if cached is not None:
        return cached
    results = _itunes_search(name, artist, album, country)
    r = _find_result(results, name, album)
    if not r and re.search(r"\(.*\)$", album):
        clean = re.sub(r"\s*\(.*\)$", "", album).strip()
        results = _itunes_search(name, artist, clean, country)
        r = _find_result(results, name, clean)
    extras: dict = {}
    if r:
        extras["artworkUrl"]        = r.get("artworkUrl100", "").replace("100x100bb", "600x600bb")
        extras["artistViewUrl"]     = r.get("artistViewUrl")
        extras["collectionViewUrl"] = r.get("collectionViewUrl")
        extras["trackViewUrl"]      = r.get("trackViewUrl")
        _extras_set(cache_key, extras)  # only cache on success
    return extras

def get_cached_extras(pid: str, country: str = "US") -> dict:
    return _extras_get(f"{pid}|{country}") or {}

def prefetch_extras(pid: str, name: str, artist: str, album: str, country: str = "US", on_done=None):
    if get_cached_extras(pid, country):
        return

    def load():
        extras = fetch_extras(pid, name, artist, album, country)
        if extras and on_done:
            on_done()

    threading.Thread(target=load, daemon=True).start()

# ── Lyrics ─────────────────────────────────────────────────────────────────────
def fetch_lyrics(pid: str, name: str, artist: str, album: str) -> dict:
    cached = _lyrics_get(pid)
    if cached is not None:
        return cached
    params = urllib.parse.urlencode({"track_name": name, "artist_name": artist, "album_name": album})
    try:
        with urllib.request.urlopen(f"https://lrclib.net/api/get?{params}", timeout=8) as r:
            data = json.loads(r.read())
            result = {"synced": data.get("syncedLyrics"), "plain": data.get("plainLyrics")}
    except Exception as e:
        log.debug("lrclib error: %s", e)
        result = {"synced": None, "plain": None}
    _lyrics_set(pid, result)
    return result

def parse_lrc(lrc_text: str) -> list:
    lines = []
    for line in lrc_text.split("\n"):
        m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)", line)
        if m:
            mins, secs, text = m.groups()
            t    = int(mins) * 60 + float(secs)
            text = text.strip()
            if text and not text.startswith("["):
                lines.append((t, text))
    return sorted(lines, key=lambda x: x[0])

def get_current_lyric(parsed_lrc: list, elapsed: float):
    if not parsed_lrc:
        return None
    current = None
    for t, text in parsed_lrc:
        if t <= elapsed:
            current = text
        else:
            break
    return current

# ── Discord custom status ──────────────────────────────────────────────────────
_status_lock = threading.Lock()
_status_next_allowed_at = 0.0
_status_last_payload = None

def _discord_patch(token: str, payload: dict):
    global _status_next_allowed_at, _status_last_payload
    if not token:
        return False
    now = time.time()
    with _status_lock:
        if payload == _status_last_payload:
            return True
        if now < _status_next_allowed_at:
            return False
        _status_next_allowed_at = now + STATUS_MIN_INTERVAL
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        "https://discord.com/api/v9/users/@me/settings",
        data=data,
        headers={
            "Authorization": token,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=1.0):
            pass
        with _status_lock:
            _status_last_payload = payload
        return True
    except urllib.error.HTTPError as e:
        retry_after = e.headers.get("Retry-After")
        if retry_after:
            try:
                wait = float(retry_after)
            except ValueError:
                wait = 30.0
        else:
            wait = 30.0 if e.code == 429 else STATUS_MIN_INTERVAL
        with _status_lock:
            _status_next_allowed_at = time.time() + wait
        log.warning("Discord status HTTP %s; retry in %.1fs", e.code, wait)
        return False
    except Exception as e:
        log.warning("Discord status error: %s", e)
        with _status_lock:
            _status_next_allowed_at = time.time() + STATUS_MIN_INTERVAL
        return False

_GAME_HIDDEN_SENTINEL = os.path.join(BASE_DIR, ".game_hidden")

def _set_show_game(token: str, show: bool):
    if not token:
        return
    data = json.dumps({"show_current_game": show}).encode("utf-8")
    req = urllib.request.Request(
        "https://discord.com/api/v9/users/@me/settings",
        data=data,
        headers={
            "Authorization": token,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0):
            pass
        if show:
            try:
                os.unlink(_GAME_HIDDEN_SENTINEL)
            except FileNotFoundError:
                pass
            log.info("Game activity restored.")
        else:
            try:
                open(_GAME_HIDDEN_SENTINEL, "w").close()
            except Exception:
                pass
            log.info("Game activity hidden.")
    except Exception as e:
        log.debug("show_current_game patch error: %s", e)

def set_discord_status(token: str, text: str, emoji: str = "\U0001f3b5"):
    custom_status = {"text": text[:128], "expires_at": None}
    if emoji:
        custom_status["emoji_name"] = emoji
    _discord_patch(token, {"custom_status": custom_status})

def clear_discord_status(token: str):
    _discord_patch(token, {"custom_status": None})

def _run_daemon(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()

def set_discord_status_async(token: str, text: str, emoji: str = "\U0001f3b5"):
    _run_daemon(set_discord_status, token, text, emoji)

def clear_discord_status_async(token: str):
    _run_daemon(clear_discord_status, token)

# ── Windows Media Session ──────────────────────────────────────────────────────
def _is_apple_music(source: str) -> bool:
    return any(k in source.lower() for k in ["applemusic", "apple music", "itunes"])

async def _get_track(mgr=None) -> dict | None:
    try:
        # Reuse a cached manager when available — avoids the expensive request_async() call
        sessions = mgr or await MediaManager.request_async()
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

    tracks = []
    for idx, session in enumerate(candidates):
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
            artist = artist.strip()
            album  = album.strip()
        if not album:
            for sep in (" — ", " – ", " - "):
                if sep in artist:
                    artist, album = artist.split(sep, 1)
                    artist = artist.strip()
                    album  = album.strip()
                    break

        position = 0.0
        duration = 0.0
        def _ticks_to_sec(v) -> float:
            if hasattr(v, "total_seconds"):
                return v.total_seconds()
            return v / 1e7
        try:
            tl = session.get_timeline_properties()
            if tl:
                position = _ticks_to_sec(tl.position)
                duration = _ticks_to_sec(tl.end_time)
        except Exception:
            pass

        tracks.append({
            "persistent_id": f"{title}|{artist}|{album}",
            "title":    title,
            "artist":   artist,
            "album":    album,
            "playing":  status == PlaybackStatus.PLAYING,
            "paused":   status == PlaybackStatus.PAUSED,
            "position": position,
            "duration": duration,
            "_session_order": idx,
        })

    if not tracks:
        return None

    playing = [t for t in tracks if t["playing"]]
    if playing:
        return playing[0]
    return tracks[0]

class MediaSessionWatcher:
    def __init__(self, wake_event: threading.Event):
        self.wake_event = wake_event
        self.manager = None
        self._manager_tokens = []
        self._session_tokens = {}

    async def start(self):
        self.manager = await MediaManager.request_async()
        self._manager_tokens.append(
            ("current_session_changed", self.manager.add_current_session_changed(self._changed))
        )
        self._manager_tokens.append(
            ("sessions_changed", self.manager.add_sessions_changed(self._changed))
        )
        self.refresh_sessions()

    def _changed(self, sender=None, args=None):
        self.wake_event.set()

    def refresh_sessions(self):
        if not self.manager:
            return
        sessions = []
        cur = self.manager.get_current_session()
        if cur:
            sessions.append(cur)
        for session in self.manager.get_sessions():
            if session not in sessions:
                sessions.append(session)
        for session in sessions:
            source = session.source_app_user_model_id or ""
            if _is_apple_music(source):
                self._register_session(session)

    def _register_session(self, session):
        key = id(session)
        if key in self._session_tokens:
            return
        self._session_tokens[key] = (
            session,
            session.add_media_properties_changed(self._changed),
            # playback_info_changed fires every ~1s for position updates — omit to avoid
            # hammering _get_track() in a tight loop; pause/stop is caught by the fallback poll
        )

# ── Activity builder ───────────────────────────────────────────────────────────
def _make_activity(track: dict, country: str = "US", lyric: str | None = None) -> dict:
    extras = get_cached_extras(track["persistent_id"], country)

    pos = track.get("position", 0.0)
    dur = track.get("duration", 0.0)
    now = time.time()

    start_ts = math.ceil(now - pos)
    end_ts   = math.ceil(now - pos + dur) if dur > 0 else None

    activity: dict = {
        "type": 2,
        "details": _ensure_len(track["title"]),
        "timestamps": {"start": start_ts},
    }
    if end_ts:
        activity["timestamps"]["end"] = end_ts

    state = lyric or track["artist"]
    if state:
        activity["status_display_type"] = 1
        activity["state"] = _ensure_len(state)

    artwork = extras.get("artworkUrl")
    if extras:
        if extras.get("trackViewUrl"):
            activity["details_url"] = extras["trackViewUrl"]
        if extras.get("artistViewUrl"):
            activity["state_url"] = extras["artistViewUrl"]
        if artwork:
            activity["assets"] = {
                "large_image": artwork,
                "large_text":  _ensure_len(track["album"] or f'{track["title"]} - {track["artist"]}'),
            }
            log.debug("Artwork: %s", artwork)
            if extras.get("collectionViewUrl"):
                activity["assets"]["large_url"] = extras["collectionViewUrl"]

    buttons = []
    yq = urllib.parse.quote(f"{track['artist']} {track['title']}")
    yu = f"https://www.youtube.com/results?search_query={yq}"
    if len(yu) <= 512:
        buttons.append({"label": "Search on YouTube", "url": yu})
    sq = urllib.parse.quote(f'artist:{track["artist"]} track:{track["title"]}')
    su = f"https://open.spotify.com/search/{sq}?si"
    if len(su) <= 512:
        buttons.append({"label": "Search on Spotify", "url": su})
    if buttons:
        activity["buttons"] = buttons[:2]

    return activity

# Send activity directly via IPC pipe (version-agnostic, supports undocumented fields)
def _rpc_set_activity(rpc, activity: dict):
    payload = {
        "cmd": "SET_ACTIVITY",
        "args": {"pid": os.getpid(), "activity": activity},
        "nonce": f"{time.time():.20f}",
    }
    rpc.send_data(1, payload)

def _next_interval(track: dict | None, cfg_interval: int) -> float:
    if track and track.get("duration") and track.get("position") is not None:
        remaining = track["duration"] - track["position"]
        if remaining > 0:
            return min(remaining + 1, cfg_interval)
    return cfg_interval

def _time_until_next_lyric(parsed_lrc: list, elapsed: float) -> float:
    """Seconds until the next lyric line starts, minus Discord display lead time."""
    for t, _ in parsed_lrc:
        if t > elapsed:
            return max(0.0, (t - elapsed) - LYRIC_DISPLAY_LEAD)
    return 30.0

def _reset_track_state(worker):
    worker._last_id    = None
    worker._parsed_lrc = None
    worker._lrc_pid    = None
    worker._last_lyric = None
    worker._last_rpc_lyric = None
    worker._lyrics_loading_id = None
    worker._last_activity_key = None
    worker._force_activity_refresh_id = None

def _clear_presence(worker, token: str = "", clear_status: bool = True):
    if worker._rpc:
        try:
            worker._rpc.clear()
        except Exception:
            pass
    if token and clear_status:
        clear_discord_status_async(token)
    worker._activity_active = False
    _reset_track_state(worker)

# ── Startup ────────────────────────────────────────────────────────────────────
_STARTUP_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_NAME = "AppleMusicRPC"

def set_startup(enabled: bool):
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_KEY, 0, winreg.KEY_SET_VALUE)
        if enabled:
            pythonw = sys.executable.replace("python.exe", "pythonw.exe")
            if not os.path.exists(pythonw):
                pythonw = sys.executable
            script = os.path.abspath(__file__)
            winreg.SetValueEx(key, _STARTUP_NAME, 0, winreg.REG_SZ, f'"{pythonw}" "{script}"')
        else:
            try:
                winreg.DeleteValue(key, _STARTUP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        log.debug("Startup registry error: %s", e)

def get_startup() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_KEY, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, _STARTUP_NAME)
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False

# ── Tray icon ──────────────────────────────────────────────────────────────────
def _make_tray_image() -> "Image.Image":
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    w   = (255, 255, 255, 255)
    d.rectangle([26, 8,  32, 50], fill=w)
    d.rectangle([50, 4,  56, 36], fill=w)
    d.rectangle([26, 8,  56, 18], fill=w)
    d.ellipse(  [ 4, 42, 32, 58], fill=w)
    d.ellipse(  [34, 32, 60, 46], fill=w)
    return img

# ── Token guide window ─────────────────────────────────────────────────────────
class TokenGuideWindow:
    _instance = None

    @classmethod
    def open(cls, root):
        if cls._instance and cls._instance.win.winfo_exists():
            cls._instance.win.lift()
            cls._instance.win.focus_force()
            return
        cls._instance = cls(root)

    def __init__(self, root):
        win = tk.Toplevel(root)
        win.title("How to get your Discord token")
        win.geometry("520x560")
        win.resizable(False, False)
        win.configure(bg="#1e1e2e")
        win.lift()
        win.focus_force()
        self.win = win
        self._build(win)

    def _build(self, win):
        tk.Label(win, text="How to get your Discord token",
                 bg="#1e1e2e", fg="#89b4fa",
                 font=("Segoe UI", 13, "bold")).pack(pady=(14, 2))
        tk.Label(win,
                 text="Required only for lyrics in Discord status. Never share your token.",
                 bg="#1e1e2e", fg="#a6adc8", font=("Segoe UI", 9),
                 justify="center").pack(pady=(0, 10))

        # ── Method 1: Network tab (always works) ──────────────────────────────
        m1 = tk.Frame(win, bg="#1e1e2e")
        m1.pack(fill="x", padx=20, pady=(0, 6))
        tk.Label(m1, text="Method 1 — Network tab  (recommended, always works)",
                 bg="#1e1e2e", fg="#a6e3a1",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")

        net_steps = [
            ("1", "Open discord.com/channels/@me in any browser"),
            ("2", "Press F12  →  click the Network tab"),
            ("3", "Press Ctrl+R to reload the page"),
            ("4", 'In the filter box type  /api/v9  and press Enter'),
            ("5", "Click any request  →  Headers  →  Request Headers"),
            ("6", 'Find  "authorization:"  — that value is your token'),
        ]
        for num, desc in net_steps:
            row = tk.Frame(m1, bg="#1e1e2e")
            row.pack(fill="x", pady=1)
            tk.Label(row, text=num, bg="#45475a", fg="#cdd6f4",
                     font=("Segoe UI", 9, "bold"), width=2,
                     padx=4, pady=2).pack(side="left", padx=(0, 8))
            tk.Label(row, text=desc, bg="#1e1e2e", fg="#cdd6f4",
                     font=("Segoe UI", 9), anchor="w").pack(side="left", fill="x")

        # ── Method 2: Console script (Chrome/Edge only) ───────────────────────
        tk.Frame(win, bg="#313244", height=1).pack(fill="x", padx=20, pady=10)

        m2 = tk.Frame(win, bg="#1e1e2e")
        m2.pack(fill="x", padx=20, pady=(0, 6))
        tk.Label(m2, text="Method 2 — Console script  (Chrome / Edge only)",
                 bg="#1e1e2e", fg="#89b4fa",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")

        con_steps = [
            ("1", "Open discord.com/channels/@me in Chrome or Edge"),
            ("2", 'Press F12  →  Console tab'),
            ("3", 'Type  allow pasting  and press Enter  (security prompt)'),
            ("4", "Paste the script below and press Enter"),
        ]
        for num, desc in con_steps:
            row = tk.Frame(m2, bg="#1e1e2e")
            row.pack(fill="x", pady=1)
            tk.Label(row, text=num, bg="#45475a", fg="#cdd6f4",
                     font=("Segoe UI", 9, "bold"), width=2,
                     padx=4, pady=2).pack(side="left", padx=(0, 8))
            tk.Label(row, text=desc, bg="#1e1e2e", fg="#cdd6f4",
                     font=("Segoe UI", 9), anchor="w").pack(side="left", fill="x")

        script_frame = tk.Frame(win, bg="#313244", bd=0)
        script_frame.pack(fill="x", padx=20, pady=(8, 2))
        script_text = tk.Text(script_frame, height=3, wrap="word",
                              bg="#313244", fg="#a6e3a1",
                              font=("Courier New", 8), relief="flat",
                              bd=8, state="normal", cursor="arrow")
        script_text.insert("1.0", TOKEN_SCRIPT)
        script_text.config(state="disabled")
        script_text.pack(fill="x")

        def copy_script():
            win.clipboard_clear()
            win.clipboard_append(TOKEN_SCRIPT)
            copy_btn.config(text="Copied! ", bg="#a6e3a1", fg="#1e1e2e")
            win.after(2000, lambda: copy_btn.config(
                text="Copy script", bg="#45475a", fg="#cdd6f4"))

        copy_btn = tk.Button(win, text="Copy script",
                             bg="#45475a", fg="#cdd6f4",
                             font=("Segoe UI", 9), relief="flat",
                             padx=12, pady=4, cursor="hand2",
                             command=copy_script)
        copy_btn.pack(pady=(2, 10))

        tk.Button(win, text="Close",
                  bg="#313244", fg="#cdd6f4",
                  font=("Segoe UI", 10), relief="flat",
                  padx=20, pady=5, cursor="hand2",
                  command=win.destroy).pack()

# ── Settings window ────────────────────────────────────────────────────────────
class SettingsWindow:
    _instance = None

    @classmethod
    def open(cls, root, cfg, on_save):
        if cls._instance and cls._instance.win.winfo_exists():
            cls._instance.win.lift()
            cls._instance.win.focus_force()
            return
        cls._instance = cls(root, cfg, on_save)

    def __init__(self, root, cfg, on_save):
        self.root    = root
        self.cfg     = cfg.copy()
        self.on_save = on_save
        win = tk.Toplevel(root)
        win.title("Apple Music RPC — Settings")
        win.geometry("500x430")
        win.resizable(False, False)
        win.configure(bg="#1e1e2e")
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.lift()
        win.focus_force()
        self.win = win
        self._build(win)

    def _build(self, win):
        s = ttk.Style()
        s.theme_use("clam")
        for w in ("TNotebook", "TFrame"):
            s.configure(w, background="#1e1e2e", borderwidth=0)
        s.configure("TNotebook.Tab", background="#313244", foreground="#cdd6f4", padding=[14, 6])
        s.map("TNotebook.Tab",
              background=[("selected", "#89b4fa")],
              foreground=[("selected", "#1e1e2e")])
        s.configure("TCombobox", fieldbackground="#313244", background="#313244",
                    foreground="#cdd6f4", selectbackground="#45475a",
                    arrowcolor="#cdd6f4")

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=12, pady=12)

        td = ttk.Frame(nb); nb.add(td, text="  Discord  ")
        tg = ttk.Frame(nb); nb.add(tg, text="  General  ")
        ta = ttk.Frame(nb); nb.add(ta, text="  About  ")

        self._build_discord(td)
        self._build_general(tg)
        self._build_about(ta)

        bf = tk.Frame(win, bg="#1e1e2e")
        bf.pack(fill="x", padx=12, pady=(0, 12))
        self._btn("#89b4fa", "#1e1e2e", "Save",   self._save).pack(side="right", padx=(4, 0))
        self._btn("#313244", "#cdd6f4", "Cancel", win.destroy).pack(side="right")

    def _btn(self, bg, fg, text, cmd):
        return tk.Button(self.win, text=text, bg=bg, fg=fg,
                         font=("Segoe UI", 10, "bold"), relief="flat",
                         padx=18, pady=5, cursor="hand2", command=cmd)

    def _row(self, parent, text, row, col=0):
        tk.Label(parent, text=text, bg="#1e1e2e", fg="#cdd6f4",
                 font=("Segoe UI", 10)).grid(row=row, column=col,
                                             sticky="w", padx=16, pady=7)

    def _check(self, parent, var, row):
        tk.Checkbutton(parent, variable=var, bg="#1e1e2e",
                       activebackground="#1e1e2e", selectcolor="#313244",
                       fg="#cdd6f4").grid(row=row, column=1,
                                          sticky="w", padx=(0, 16), pady=7)

    def _build_discord(self, f):
        f.columnconfigure(1, weight=1)

        self._row(f, "Token:", 0)
        tf = tk.Frame(f, bg="#1e1e2e")
        tf.grid(row=0, column=1, sticky="ew", padx=(0, 16), pady=7)

        self._tok_var   = tk.StringVar(value=self.cfg.get("discord_token", ""))
        self._tok_entry = tk.Entry(tf, textvariable=self._tok_var, show="•",
                                   bg="#313244", fg="#cdd6f4",
                                   insertbackground="#cdd6f4",
                                   relief="flat", bd=6, font=("Segoe UI", 9))
        self._tok_entry.pack(side="left", fill="x", expand=True)

        self._eye_btn = tk.Button(tf, text="Show", bg="#45475a", fg="#cdd6f4",
                                  relief="flat", padx=6, cursor="hand2",
                                  font=("Segoe UI", 8),
                                  command=self._toggle_token)
        self._eye_btn.pack(side="left", padx=(4, 0))

        tk.Button(f, text="How to get your token →",
                  bg="#1e1e2e", fg="#89b4fa",
                  font=("Segoe UI", 9, "underline"), relief="flat",
                  cursor="hand2", anchor="w",
                  command=lambda: TokenGuideWindow.open(self.root)
                  ).grid(row=1, column=0, columnspan=2, sticky="w",
                         padx=12, pady=(0, 4))

        self._row(f, "Enable Rich Presence:", 2)
        self._rpc_var = tk.BooleanVar(value=self.cfg.get("rpc_enabled", True))
        self._check(f, self._rpc_var, 2)

        self._row(f, "Lyrics in status:", 3)
        self._lyr_var = tk.BooleanVar(value=self.cfg.get("lyrics_in_status", True))
        self._check(f, self._lyr_var, 3)

        self._row(f, "Lyrics in Rich Presence:", 4)
        self._rpc_lyr_var = tk.BooleanVar(value=self.cfg.get("lyrics_in_rpc", True))
        self._check(f, self._rpc_lyr_var, 4)

        self._row(f, "Hide game activity:", 5)
        self._hide_game_var = tk.BooleanVar(value=self.cfg.get("hide_game_activity", False))
        self._check(f, self._hide_game_var, 5)

        self._row(f, "Lyrics emoji:", 6)
        self._emoji_var = tk.StringVar(value=self.cfg.get("lyrics_emoji", "\U0001f3b5"))
        tk.Entry(f, textvariable=self._emoji_var, width=5,
                 bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4",
                 relief="flat", bd=6, font=("Segoe UI", 13)).grid(
                     row=6, column=1, sticky="w", padx=(0, 16), pady=7)

    def _build_general(self, f):
        f.columnconfigure(1, weight=1)

        self._row(f, "Poll interval (sec):", 0)
        self._poll_var = tk.IntVar(value=self.cfg.get("poll_interval", DEFAULT_TIMEOUT))
        tk.Spinbox(f, from_=1, to=60, textvariable=self._poll_var, width=6,
                   bg="#313244", fg="#cdd6f4", buttonbackground="#45475a",
                   relief="flat", font=("Segoe UI", 10)).grid(
                       row=0, column=1, sticky="w", padx=(0, 16), pady=7)

        self._row(f, "iTunes store:", 1)
        country_codes  = [c for c, _ in ITUNES_COUNTRIES]
        country_labels = [f"{name} ({code})" for code, name in ITUNES_COUNTRIES]
        self._country_var = tk.StringVar()
        cur_code = self.cfg.get("itunes_country", "US")
        try:
            idx = country_codes.index(cur_code)
            self._country_var.set(country_labels[idx])
        except ValueError:
            self._country_var.set(country_labels[0])
        self._country_codes  = country_codes
        self._country_labels = country_labels
        combo = ttk.Combobox(f, textvariable=self._country_var,
                             values=country_labels, state="readonly",
                             width=22, font=("Segoe UI", 9))
        combo.grid(row=1, column=1, sticky="w", padx=(0, 16), pady=7)

        self._row(f, "Start with Windows:", 2)
        self._start_var = tk.BooleanVar(value=get_startup())
        self._check(f, self._start_var, 2)

        tk.Label(f, text="The poll interval controls how often the app\nchecks which track is playing.",
                 bg="#1e1e2e", fg="#585b70",
                 font=("Segoe UI", 8), justify="left").grid(
                     row=3, column=0, columnspan=2,
                     sticky="w", padx=16, pady=(8, 0))

    def _build_about(self, f):
        tk.Label(f, text="\U0001f3b5", bg="#1e1e2e", fg="#89b4fa",
                 font=("Segoe UI", 36)).pack(pady=(20, 4))
        tk.Label(f, text=f"Apple Music Discord RPC  v{VERSION}",
                 bg="#1e1e2e", fg="#cdd6f4",
                 font=("Segoe UI", 13, "bold")).pack()
        tk.Label(f, text="Windows port — based on NextFire/apple-music-discord-rpc",
                 bg="#1e1e2e", fg="#585b70", font=("Segoe UI", 9)).pack(pady=4)
        tk.Label(f, text="github.com/spxmiguel/apple-music-discord-rpc",
                 bg="#1e1e2e", fg="#89b4fa", font=("Segoe UI", 9)).pack()

    def _toggle_token(self):
        showing = self._tok_entry.cget("show") == ""
        self._tok_entry.config(show="•" if showing else "")
        self._eye_btn.config(text="Show" if showing else "Hide")

    def _save(self):
        # Resolve country code from label
        label = self._country_var.get()
        try:
            idx = self._country_labels.index(label)
            country = self._country_codes[idx]
        except ValueError:
            country = "US"

        self.cfg["discord_token"]      = self._tok_var.get().strip()
        self.cfg["rpc_enabled"]        = self._rpc_var.get()
        self.cfg["lyrics_in_status"]   = self._lyr_var.get()
        self.cfg["lyrics_in_rpc"]      = self._rpc_lyr_var.get()
        self.cfg["hide_game_activity"] = self._hide_game_var.get()
        self.cfg["lyrics_emoji"]       = self._emoji_var.get()
        self.cfg["poll_interval"]      = self._poll_var.get()
        self.cfg["start_with_windows"] = self._start_var.get()
        self.cfg["itunes_country"]     = country
        set_startup(self.cfg["start_with_windows"])
        save_config(self.cfg)
        self.on_save(self.cfg)
        messagebox.showinfo("Saved", "Settings saved!", parent=self.win)
        self.win.destroy()

# ── RPC worker ─────────────────────────────────────────────────────────────────
class RPCWorker:
    def __init__(self, cfg_getter):
        self.cfg_getter  = cfg_getter
        self._rpc        = None
        self._last_id    = None
        self._last_lyric = None
        self._last_rpc_lyric = None
        self._parsed_lrc = None
        self._lrc_pid    = None
        self._lyrics_loading_id = None
        self._start_time = time.time()
        self._stop       = threading.Event()
        self._media_changed = threading.Event()
        self._loop       = None
        self._watcher    = None
        self._activity_active = False
        self._last_activity_key = None
        self._force_activity_refresh_id = None
        self._last_session_refresh = 0.0
        self._game_hidden = False

    def _send_rpc_lyric(self, track: dict, country: str, lyric: str):
        if not self._rpc or not self._activity_active:
            return
        if lyric == self._last_rpc_lyric:
            return
        _rpc_set_activity(self._rpc, _make_activity(track, country, lyric))
        self._last_rpc_lyric = lyric
        log.info("RPC lyric: %s", lyric[:60])

    def _start_lyrics_load(self, track: dict):
        pid = track["persistent_id"]
        if self._lrc_pid == pid or self._lyrics_loading_id == pid:
            return
        self._lyrics_loading_id = pid

        def load():
            try:
                ly = fetch_lyrics(pid, track["title"], track["artist"], track["album"])
                synced = ly.get("synced")
                parsed = parse_lrc(synced) if synced else []
                if parsed:
                    log.info("Lyrics loaded: %s lines for %s", len(parsed), pid)
                else:
                    log.info("No synced lyrics found for %s", pid)
            except Exception as e:
                log.debug("Lyrics load error: %s", e)
                parsed = []
            if self._last_id == pid:
                self._parsed_lrc = parsed
                self._lrc_pid = pid
                self._media_changed.set()
            if self._lyrics_loading_id == pid:
                self._lyrics_loading_id = None

        threading.Thread(target=load, daemon=True).start()

    def _schedule_timing_refresh(self, pid: str):
        def refresh():
            time.sleep(0.8)
            if self._last_id == pid and not self._stop.is_set():
                self._force_activity_refresh_id = pid
                self._media_changed.set()

        threading.Thread(target=refresh, daemon=True).start()

    def stop(self):
        self._stop.set()
        self._media_changed.set()
        if self._game_hidden:
            token = self.cfg_getter().get("discord_token", "")
            if token:
                _set_show_game(token, True)
            self._game_hidden = False

    def _wait_for_media_event(self, timeout: float) -> bool:
        """Sleep up to timeout seconds. Returns True if a media event fired early."""
        timeout = max(0.0, float(timeout))
        self._media_changed.clear()
        fired = self._media_changed.wait(timeout)
        if fired:
            self._media_changed.clear()
        return fired

    def run(self):
        # Initialize COM as Single-Threaded Apartment for WinRT in this thread
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0)
        except Exception:
            pass

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            try:
                self._watcher = MediaSessionWatcher(self._media_changed)
                self._loop.run_until_complete(self._watcher.start())
                log.info("Watching Windows Media Session events.")
            except Exception as e:
                log.warning("Media event watcher unavailable, using fallback polling: %s", e)
            self._main_loop()
        finally:
            self._loop.close()
            try:
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass

    def _main_loop(self):
        # Restore show_current_game if a previous run crashed while hiding it
        _startup_cfg = self.cfg_getter()
        _startup_token = _startup_cfg.get("discord_token", "")
        if _startup_token and os.path.exists(_GAME_HIDDEN_SENTINEL):
            _run_daemon(_set_show_game, _startup_token, True)

        while not self._stop.is_set():
            if time.time() - self._start_time >= MAX_RUNTIME:
                log.info("Max runtime reached, restarting...")
                os.execv(sys.executable, [sys.executable] + sys.argv)

            cfg       = self.cfg_getter()
            interval  = max(FAST_CHECK_INTERVAL, float(cfg.get("poll_interval", DEFAULT_TIMEOUT)))
            token     = cfg.get("discord_token", "")
            rpc_on    = cfg.get("rpc_enabled", True)
            use_lyr   = cfg.get("lyrics_in_status", True)
            rpc_lyr   = cfg.get("lyrics_in_rpc", True)
            emoji     = cfg.get("lyrics_emoji", "\U0001f3b5")
            country   = cfg.get("itunes_country", "US")
            hide_game = cfg.get("hide_game_activity", False)

            if not rpc_on and self._rpc is not None:
                try:
                    self._rpc.clear()
                    self._rpc.close()
                except Exception:
                    pass
                self._rpc     = None
                _reset_track_state(self)

            if rpc_on and self._rpc is None:
                try:
                    self._rpc = Presence(CLIENT_ID)
                    self._rpc.connect()
                    log.info("Connected to Discord RPC.")
                except InvalidPipe:
                    log.warning("Discord not running, retry in %.1fs...", interval)
                    self._stop.wait(min(interval, INACTIVE_CHECK_INTERVAL))
                    continue
                except Exception as e:
                    log.error("RPC connect error: %s", e)
                    self._rpc = None
                    self._stop.wait(min(interval, INACTIVE_CHECK_INTERVAL))
                    continue

            try:
                _now = time.time()
                if self._watcher and _now - self._last_session_refresh > 5.0:
                    self._watcher.refresh_sessions()
                    self._last_session_refresh = _now
                _mgr  = self._watcher.manager if self._watcher else None
                track = self._loop.run_until_complete(_get_track(_mgr))
            except Exception as e:
                log.debug("Track fetch error: %s", e)
                track = None

            try:
                if not track or not track["playing"]:
                    reason = "paused" if (track and track["paused"]) else "stopped"
                    if self._activity_active or self._last_id is not None:
                        log.info("Cleared (%s).", reason)
                    _clear_presence(self, token, token and use_lyr)
                else:
                    pid = track["persistent_id"]

                    if self._rpc:
                        extras = get_cached_extras(pid, country)
                        activity_key = (pid, extras.get("artworkUrl", ""), round(track.get("duration", 0.0)))
                        force_activity_refresh = self._force_activity_refresh_id == pid
                        should_update_activity = (
                            pid != self._last_id
                            or activity_key != self._last_activity_key
                            or force_activity_refresh
                        )
                        if should_update_activity:
                            _rpc_set_activity(self._rpc, _make_activity(track, country))
                            self._activity_active = True
                            self._last_activity_key = activity_key
                            if force_activity_refresh:
                                self._force_activity_refresh_id = None
                            log.info("RPC update sent: %s", pid)
                        if pid != self._last_id:
                            log.info("Playing: %s — %s", track["title"], track["artist"])
                            self._schedule_timing_refresh(pid)
                            prefetch_extras(
                                pid,
                                track["title"],
                                track["artist"],
                                track["album"],
                                country,
                                self._media_changed.set,
                            )
                            self._last_id    = pid
                            self._parsed_lrc = None
                            self._lrc_pid    = None
                            self._last_lyric = None
                            self._last_rpc_lyric = None
                            if token and use_lyr:
                                clear_discord_status_async(token)
                    elif pid != self._last_id:
                        log.info("Playing (RPC off): %s — %s", track["title"], track["artist"])
                        self._last_id    = pid
                        self._parsed_lrc = None
                        self._lrc_pid    = None
                        self._last_lyric = None
                        self._last_rpc_lyric = None
                        if token and use_lyr:
                            clear_discord_status_async(token)

                    if use_lyr or rpc_lyr:
                        if self._lrc_pid != pid:
                            self._start_lyrics_load(track)

                        lyric = get_current_lyric(self._parsed_lrc, track.get("position", 0))
                        if lyric and lyric != self._last_lyric:
                            if rpc_lyr:
                                self._send_rpc_lyric(track, country, lyric)
                            if token and use_lyr:
                                _run_daemon(set_discord_status, token, lyric, emoji)
                            log.info("Lyric: %s", lyric[:60])
                            self._last_lyric = lyric

            except InvalidPipe:
                log.warning("Lost Discord connection, reconnecting...")
                self._rpc     = None
                self._activity_active = False
                _reset_track_state(self)
            except Exception as e:
                log.error("Presence update error: %s", e)
                self._wait_for_media_event(INACTIVE_CHECK_INTERVAL)

            # Toggle game activity visibility
            should_hide = hide_game and bool(track and track["playing"]) and bool(token)
            if should_hide and not self._game_hidden:
                _run_daemon(_set_show_game, token, False)
                self._game_hidden = True
            elif not should_hide and self._game_hidden:
                _run_daemon(_set_show_game, token, True)
                self._game_hidden = False

            if not track or not track["playing"]:
                self._wait_for_media_event(INACTIVE_CHECK_INTERVAL)
                continue

            # Lyrics path: precise timing loop with estimated position.
            # No-lyrics path: single wait, zero WinRT calls — outer loop handles state.
            has_lyrics = bool((use_lyr or rpc_lyr) and self._parsed_lrc and self._last_id)
            if has_lyrics:
                pos0     = track.get("position", 0.0)
                t0       = time.time()
                deadline = t0 + EVENT_FALLBACK_INTERVAL
                while not self._stop.is_set() and time.time() < deadline:
                    est_pos = pos0 + (time.time() - t0)
                    lyric = get_current_lyric(self._parsed_lrc, est_pos)
                    if lyric and lyric != self._last_lyric:
                        if rpc_lyr:
                            self._send_rpc_lyric(track, country, lyric)
                        if token and use_lyr:
                            set_discord_status(token, lyric, emoji)
                        log.info("Lyric: %s", lyric[:60])
                        self._last_lyric = lyric
                    wait = min(_time_until_next_lyric(self._parsed_lrc, est_pos),
                               EVENT_FALLBACK_INTERVAL, deadline - time.time())
                    if wait <= 0:
                        break
                    fired = self._wait_for_media_event(wait)
                    if fired:
                        try:
                            _mgr = self._watcher.manager if self._watcher else None
                            t = self._loop.run_until_complete(_get_track(_mgr))
                        except Exception:
                            break
                        if not t or not t["playing"] or t["persistent_id"] != self._last_id:
                            break
                        pos0, t0, track = t.get("position", 0.0), time.time(), t
            else:
                # No lyrics active: one sleep, no _get_track() at all.
                # An early event (song change, stop) wakes us so the outer loop
                # re-checks promptly; otherwise we wait the full fallback interval.
                self._wait_for_media_event(EVENT_FALLBACK_INTERVAL)

        cfg   = self.cfg_getter()
        token = cfg.get("discord_token", "")
        if token:
            clear_discord_status_async(token)
        if self._game_hidden and token:
            _set_show_game(token, True)
            self._game_hidden = False
        if self._rpc:
            try:
                self._rpc.clear()
                self._rpc.close()
            except Exception:
                pass

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    # Run at below-normal priority so we never compete with games for CPU
    try:
        handle = ctypes.windll.kernel32.OpenProcess(0x0200, False, os.getpid())
        ctypes.windll.kernel32.SetPriorityClass(handle, 0x00004000)  # BELOW_NORMAL_PRIORITY_CLASS
        ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass

    cfg_lock = threading.Lock()
    _cfg     = [load_config()]

    def get_cfg():
        with cfg_lock:
            return _cfg[0].copy()

    def set_cfg(new_cfg):
        with cfg_lock:
            _cfg[0] = new_cfg
        # Always clear custom status on save so a stale lyric never lingers;
        # it will be re-sent on the next loop cycle if lyrics are still enabled
        token = new_cfg.get("discord_token", "")
        if token:
            clear_discord_status_async(token)
        worker._last_lyric = None
        worker._last_rpc_lyric = None
        worker._media_changed.set()

    root = tk.Tk()
    root.withdraw()
    root.title("Apple Music RPC")

    gui_q = queue.Queue()

    def check_queue():
        try:
            while True:
                msg = gui_q.get_nowait()
                if msg == "settings":
                    SettingsWindow.open(root, get_cfg(), set_cfg)
                elif msg == "quit":
                    worker.stop()
                    if HAS_TRAY and tray:
                        try:
                            tray.stop()
                        except Exception:
                            pass
                    root.quit()
        except queue.Empty:
            pass
        root.after(100, check_queue)

    root.after(100, check_queue)

    worker = RPCWorker(get_cfg)
    threading.Thread(target=worker.run, daemon=True).start()

    tray = None
    if HAS_TRAY:
        menu = pystray.Menu(
            pystray.MenuItem("Settings",
                             lambda icon, item: gui_q.put("settings")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda icon, item: gui_q.put("quit")),
        )
        tray = pystray.Icon("apple-music-rpc", _make_tray_image(), "Apple Music RPC", menu)
        threading.Thread(target=tray.run, daemon=True).start()

    if not get_cfg().get("discord_token"):
        root.after(600, lambda: gui_q.put("settings"))

    root.mainloop()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
