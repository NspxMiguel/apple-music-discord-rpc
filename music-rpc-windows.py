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
import re
import threading
import queue
import winreg
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

CLIENT_ID   = "773825528921849856"
VERSION     = "1.3"
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
CACHE_FILE  = os.path.join(BASE_DIR, "cache.sqlite3")

DEFAULT_CONFIG = {
    "discord_token": "",
    "lyrics_in_status": True,
    "lyrics_emoji": "\U0001f3b5",
    "poll_interval": 5,
    "start_with_windows": True,
}

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
    con = sqlite3.connect(CACHE_FILE)
    con.execute("CREATE TABLE IF NOT EXISTS extras (id TEXT PRIMARY KEY, data TEXT, expires_at INTEGER)")
    con.execute("CREATE TABLE IF NOT EXISTS lyrics  (id TEXT PRIMARY KEY, data TEXT)")
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
    _db.execute("INSERT OR REPLACE INTO extras(id,data,expires_at) VALUES(?,?,?)",
                (pid, json.dumps(extras), extras.get("expiresAt")))
    _db.commit()

def _lyrics_get(pid):
    row = _db.execute("SELECT data FROM lyrics WHERE id=?", (pid,)).fetchone()
    return json.loads(row[0]) if row else None

def _lyrics_set(pid, data):
    _db.execute("INSERT OR REPLACE INTO lyrics(id,data) VALUES(?,?)",
                (pid, json.dumps(data)))
    _db.commit()

# ── iTunes Search ──────────────────────────────────────────────────────────────
def _itunes_search(name, artist, album):
    params = urllib.parse.urlencode({
        "media": "music", "entity": "song",
        "term": f"{name} {artist} {album}", "country": "US",
    })
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
                  if cll in r.get("collectionName", "").lower()
                  and nl  in r.get("trackName", "").lower()
                  and al  in r.get("artistName", "").lower()), None)
    if exact:
        return exact
    return next((r for r in results
                 if nl in r.get("trackName", "").lower()
                 and al in r.get("artistName", "").lower()), None)

def fetch_extras(pid, name, artist, album):
    cached = _cache_get(pid)
    if cached is not None:
        return cached
    results = _itunes_search(name, artist, album)
    r = _find_result(results, name, artist, album)
    if not r and "(" in album:
        clean = album[:album.rfind("(")].strip()
        results = _itunes_search(name, artist, clean)
        r = _find_result(results, name, artist, clean)
    extras = {}
    if r:
        extras["artworkUrl"]        = r.get("artworkUrl100", "").replace("100x100bb", "600x600bb")
        extras["artistViewUrl"]     = r.get("artistViewUrl")
        extras["collectionViewUrl"] = r.get("collectionViewUrl")
        extras["trackViewUrl"]      = r.get("trackViewUrl")
    _cache_set(pid, extras)
    return extras

# ── Lyrics (lrclib.net) ────────────────────────────────────────────────────────
def fetch_lyrics(pid, name, artist, album):
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

def parse_lrc(lrc_text):
    lines = []
    for line in lrc_text.split("\n"):
        m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)", line)
        if m:
            mins, secs, text = m.groups()
            t = int(mins) * 60 + float(secs)
            text = text.strip()
            if text and not text.startswith("["):
                lines.append((t, text))
    return sorted(lines, key=lambda x: x[0])

def get_current_lyric(parsed_lrc, elapsed):
    current = None
    for t, text in parsed_lrc:
        if t <= elapsed:
            current = text
        else:
            break
    return current

# ── Discord custom status ──────────────────────────────────────────────────────
def _discord_patch(token, payload):
    if not token:
        return
    data = json.dumps(payload).encode("utf-8")
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
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception as e:
        log.debug("Discord status error: %s", e)

def set_discord_status(token, text, emoji="\U0001f3b5"):
    _discord_patch(token, {"custom_status": {
        "text": text[:128], "emoji_name": emoji, "expires_at": None,
    }})

def clear_discord_status(token):
    _discord_patch(token, {"custom_status": None})

# ── Windows Media Session ──────────────────────────────────────────────────────
def _is_apple_music(source):
    return any(k in source.lower() for k in ["applemusic", "apple music", "itunes"])

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
            artist = artist.strip()
            album  = album.strip()

        position = 0.0
        duration = 0.0
        try:
            tl = session.get_timeline_properties()
            if tl:
                position = tl.position / 1e7
                duration = tl.end_time / 1e7
        except Exception:
            pass

        return {
            "persistent_id": f"{title}|{artist}|{album}",
            "title":    title,
            "artist":   artist,
            "album":    album,
            "playing":  status == PlaybackStatus.PLAYING,
            "paused":   status == PlaybackStatus.PAUSED,
            "position": position,
            "duration": duration,
        }
    return None

# ── Presence builder ───────────────────────────────────────────────────────────
def _make_activity(track):
    extras = fetch_extras(track["persistent_id"], track["title"], track["artist"], track["album"])

    now = time.time()
    pos = track.get("position", 0.0)
    dur = track.get("duration", 0.0)

    activity = {
        "type": 2,  # Listening to Apple Music
        "details": track["title"][:128],
        "state":   (track["artist"] or "Unknown Artist")[:128],
    }

    # Progress bar: use real playback position + duration
    if dur > 0:
        activity["timestamps"] = {
            "start": int(now - pos),
            "end":   int(now - pos + dur),
        }
    else:
        activity["timestamps"] = {"start": int(now)}

    artwork = extras.get("artworkUrl")
    if artwork:
        activity["assets"] = {
            "large_image": artwork,
            "large_text":  (track["album"] or "Apple Music")[:128],
        }

    buttons = []
    if extras.get("trackViewUrl"):
        buttons.append({"label": "Open in Apple Music", "url": extras["trackViewUrl"]})
    sq = urllib.parse.quote(f'artist:{track["artist"]} track:{track["title"]}')
    su = f"https://open.spotify.com/search/{sq}?si"
    if len(su) <= 512:
        buttons.append({"label": "Search on Spotify", "url": su})
    if buttons:
        activity["buttons"] = buttons[:2]

    return activity

# ── Startup (registry, no VBS needed) ─────────────────────────────────────────
_STARTUP_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_NAME = "AppleMusicRPC"

def set_startup(enabled):
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

def get_startup():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_KEY, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, _STARTUP_NAME)
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False

# ── Tray icon ──────────────────────────────────────────────────────────────────
def _make_tray_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = (255, 255, 255, 255)
    d.rectangle([26, 8,  32, 50], fill=w)
    d.rectangle([50, 4,  56, 36], fill=w)
    d.rectangle([26, 8,  56, 18], fill=w)
    d.ellipse(  [ 4, 42, 32, 58], fill=w)
    d.ellipse(  [34, 32, 60, 46], fill=w)
    return img

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
        self.cfg     = cfg.copy()
        self.on_save = on_save
        win = tk.Toplevel(root)
        win.title("Apple Music RPC — Configurações")
        win.geometry("480x400")
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
        s.map("TNotebook.Tab", background=[("selected", "#89b4fa")],
              foreground=[("selected", "#1e1e2e")])
        s.configure("TLabel", background="#1e1e2e", foreground="#cdd6f4", font=("Segoe UI", 10))

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=12, pady=12)

        t_discord = ttk.Frame(nb); nb.add(t_discord, text="  Discord  ")
        t_general = ttk.Frame(nb); nb.add(t_general, text="  Geral  ")
        t_about   = ttk.Frame(nb); nb.add(t_about,   text="  Sobre  ")

        self._build_discord(t_discord)
        self._build_general(t_general)
        self._build_about(t_about)

        bf = tk.Frame(win, bg="#1e1e2e")
        bf.pack(fill="x", padx=12, pady=(0, 12))
        self._btn("#89b4fa", "#1e1e2e", "Salvar",   self._save).pack(side="right", padx=(4, 0))
        self._btn("#313244", "#cdd6f4", "Cancelar", self.win.destroy).pack(side="right")

    def _btn(self, bg, fg, text, cmd):
        return tk.Button(self.win, text=text, bg=bg, fg=fg,
                         font=("Segoe UI", 10, "bold"), relief="flat",
                         padx=18, pady=5, cursor="hand2", command=cmd)

    def _row(self, parent, text, row):
        tk.Label(parent, text=text, bg="#1e1e2e", fg="#cdd6f4",
                 font=("Segoe UI", 10)).grid(row=row, column=0, sticky="w", padx=16, pady=8)

    def _build_discord(self, f):
        f.columnconfigure(1, weight=1)

        self._row(f, "Token do Discord:", 0)
        tf = tk.Frame(f, bg="#1e1e2e")
        tf.grid(row=0, column=1, sticky="ew", padx=(0, 16), pady=8)
        self._tok_var = tk.StringVar(value=self.cfg.get("discord_token", ""))
        self._tok_entry = tk.Entry(tf, textvariable=self._tok_var, show="•",
                                   bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4",
                                   relief="flat", bd=6, font=("Segoe UI", 9))
        self._tok_entry.pack(side="left", fill="x", expand=True)
        tk.Button(tf, text="\U0001f441", bg="#45475a", fg="#cdd6f4", relief="flat",
                  padx=6, cursor="hand2", command=self._toggle_token).pack(side="left", padx=(4, 0))

        self._row(f, "Letra no status:", 1)
        self._lyr_var = tk.BooleanVar(value=self.cfg.get("lyrics_in_status", True))
        tk.Checkbutton(f, variable=self._lyr_var, bg="#1e1e2e",
                       activebackground="#1e1e2e", selectcolor="#313244",
                       fg="#cdd6f4").grid(row=1, column=1, sticky="w", padx=(0, 16), pady=8)

        self._row(f, "Emoji:", 2)
        self._emoji_var = tk.StringVar(value=self.cfg.get("lyrics_emoji", "\U0001f3b5"))
        tk.Entry(f, textvariable=self._emoji_var, width=5,
                 bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4",
                 relief="flat", bd=6, font=("Segoe UI", 13)).grid(
                     row=2, column=1, sticky="w", padx=(0, 16), pady=8)

        tk.Label(f,
                 text='Como obter o token: Discord (web) → F12 → Console\ncole: (webpackChunkdiscord_app.push([[Math.random()],{},'
                      '({require:e})=>{Object.values(e.c).forEach(x=>{if(x?.exports?.default?.getToken)console.log(x.exports.default.getToken())})}]),0)',
                 bg="#1e1e2e", fg="#585b70", font=("Courier New", 7),
                 wraplength=300, justify="left").grid(row=3, column=0, columnspan=2,
                                                      sticky="w", padx=16, pady=(0, 4))

    def _build_general(self, f):
        f.columnconfigure(1, weight=1)

        self._row(f, "Intervalo de poll (seg):", 0)
        self._poll_var = tk.IntVar(value=self.cfg.get("poll_interval", 5))
        tk.Spinbox(f, from_=2, to=60, textvariable=self._poll_var, width=6,
                   bg="#313244", fg="#cdd6f4", buttonbackground="#45475a",
                   relief="flat", font=("Segoe UI", 10)).grid(
                       row=0, column=1, sticky="w", padx=(0, 16), pady=8)

        self._row(f, "Iniciar com o Windows:", 1)
        self._start_var = tk.BooleanVar(value=get_startup())
        tk.Checkbutton(f, variable=self._start_var, bg="#1e1e2e",
                       activebackground="#1e1e2e", selectcolor="#313244",
                       fg="#cdd6f4").grid(row=1, column=1, sticky="w", padx=(0, 16), pady=8)

    def _build_about(self, f):
        tk.Label(f, text="\U0001f3b5", bg="#1e1e2e", fg="#89b4fa",
                 font=("Segoe UI", 36)).pack(pady=(20, 4))
        tk.Label(f, text=f"Apple Music Discord RPC  v{VERSION}",
                 bg="#1e1e2e", fg="#cdd6f4",
                 font=("Segoe UI", 13, "bold")).pack()
        tk.Label(f, text="Port Windows — baseado em NextFire/apple-music-discord-rpc",
                 bg="#1e1e2e", fg="#585b70", font=("Segoe UI", 9)).pack(pady=4)
        tk.Label(f, text="github.com/spxmiguel/apple-music-discord-rpc",
                 bg="#1e1e2e", fg="#89b4fa", font=("Segoe UI", 9)).pack()

    def _toggle_token(self):
        self._tok_entry.config(show="" if self._tok_entry.cget("show") == "•" else "•")

    def _save(self):
        self.cfg["discord_token"]      = self._tok_var.get().strip()
        self.cfg["lyrics_in_status"]   = self._lyr_var.get()
        self.cfg["lyrics_emoji"]       = self._emoji_var.get()
        self.cfg["poll_interval"]      = self._poll_var.get()
        self.cfg["start_with_windows"] = self._start_var.get()
        set_startup(self.cfg["start_with_windows"])
        save_config(self.cfg)
        self.on_save(self.cfg)
        messagebox.showinfo("Salvo", "Configurações salvas!", parent=self.win)
        self.win.destroy()

# ── RPC worker ─────────────────────────────────────────────────────────────────
class RPCWorker:
    def __init__(self, cfg_getter):
        self.cfg_getter  = cfg_getter
        self._rpc        = None
        self._last_id    = None
        self._last_lyric = None
        self._parsed_lrc = None
        self._lrc_pid    = None
        self._stop       = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            cfg      = self.cfg_getter()
            interval = cfg.get("poll_interval", 5)
            token    = cfg.get("discord_token", "")
            use_lyr  = cfg.get("lyrics_in_status", True)
            emoji    = cfg.get("lyrics_emoji", "\U0001f3b5")

            if self._rpc is None:
                try:
                    self._rpc = Presence(CLIENT_ID)
                    self._rpc.connect()
                    log.info("Connected to Discord RPC.")
                except InvalidPipe:
                    log.warning("Discord not running, retry in %ds...", interval)
                    self._stop.wait(interval)
                    continue
                except Exception as e:
                    log.error("RPC connect error: %s", e)
                    self._rpc = None
                    self._stop.wait(interval)
                    continue

            try:
                track = asyncio.run(_get_track())
            except Exception as e:
                log.debug("Track fetch error: %s", e)
                track = None

            try:
                if not track or not track["playing"]:
                    if self._last_id is not None:
                        self._rpc.clear()
                        reason = "paused" if (track and track["paused"]) else "stopped"
                        log.info("Cleared (%s).", reason)
                        self._last_id    = None
                        self._parsed_lrc = None
                        self._lrc_pid    = None
                        self._last_lyric = None
                        if token and use_lyr:
                            clear_discord_status(token)
                else:
                    pid = track["persistent_id"]

                    if pid != self._last_id:
                        # Pass full activity dict — type 2 = "Listening to"
                        self._rpc.update(activity=_make_activity(track))
                        log.info("Playing: %s — %s", track["title"], track["artist"])
                        self._last_id    = pid
                        self._parsed_lrc = None
                        self._lrc_pid    = None
                        self._last_lyric = None
                    else:
                        # Refresh timestamps every poll so progress stays accurate
                        self._rpc.update(activity=_make_activity(track))

                    if token and use_lyr:
                        if self._lrc_pid != pid:
                            ly = fetch_lyrics(pid, track["title"], track["artist"], track["album"])
                            synced = ly.get("synced")
                            self._parsed_lrc = parse_lrc(synced) if synced else []
                            self._lrc_pid    = pid

                        lyric = get_current_lyric(self._parsed_lrc, track.get("position", 0))
                        if lyric and lyric != self._last_lyric:
                            set_discord_status(token, lyric, emoji)
                            log.info("Lyric: %s", lyric[:60])
                            self._last_lyric = lyric

            except InvalidPipe:
                log.warning("Lost Discord connection, reconnecting...")
                self._rpc     = None
                self._last_id = None
            except Exception as e:
                log.error("Presence update error: %s", e)

            self._stop.wait(interval)

        cfg   = self.cfg_getter()
        token = cfg.get("discord_token", "")
        if token:
            clear_discord_status(token)
        if self._rpc:
            try:
                self._rpc.clear()
                self._rpc.close()
            except Exception:
                pass

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    cfg_lock = threading.Lock()
    _cfg     = [load_config()]

    def get_cfg():
        with cfg_lock:
            return _cfg[0].copy()

    def set_cfg(new_cfg):
        with cfg_lock:
            _cfg[0] = new_cfg

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
            pystray.MenuItem("Configurações",
                             lambda icon, item: gui_q.put("settings")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Sair", lambda icon, item: gui_q.put("quit")),
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
