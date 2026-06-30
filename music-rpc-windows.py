#!/usr/bin/env python3
"""
Apple Music Discord Rich Presence — Windows port
Based on https://github.com/NextFire/apple-music-discord-rpc
Uses Windows Media Session API instead of JXA/osascript.
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

CLIENT_ID    = "773825528921849856"  # DO NOT CHANGE — same as NextFire's Music app
POLL_INTERVAL = 5
CACHE_FILE   = os.path.join(os.path.dirname(__file__), "cache.sqlite3")

_cache: dict[str, dict] = {}


# ── SQLite cache (mirrors Deno KV from original) ──────────────────────────────

def _init_db():
    con = sqlite3.connect(CACHE_FILE)
    con.execute("CREATE TABLE IF NOT EXISTS extras (id TEXT PRIMARY KEY, data TEXT, expires_at INTEGER)")
    con.commit()
    return con

_db = _init_db()


def _cache_get(persistent_id: str) -> dict | None:
    row = _db.execute(
        "SELECT data, expires_at FROM extras WHERE id=?", (persistent_id,)
    ).fetchone()
    if not row:
        return None
    data, expires_at = row
    if expires_at and expires_at < int(time.time() * 1000):
        return None
    return json.loads(data)


def _cache_set(persistent_id: str, extras: dict):
    _db.execute(
        "INSERT OR REPLACE INTO extras(id, data, expires_at) VALUES(?,?,?)",
        (persistent_id, json.dumps(extras), extras.get("expiresAt")),
    )
    _db.commit()


# ── iTunes Search API ──────────────────────────────────────────────────────────

def _itunes_search(name: str, artist: str, album: str) -> list:
    params = urllib.parse.urlencode({
        "media": "music",
        "entity": "song",
        "term": f"{name} {artist} {album}",
        "country": "JP",
    })
    url = f"https://itunes.apple.com/search?{params}"
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            return json.loads(r.read()).get("results", [])
    except Exception as e:
        log.debug("iTunes search error: %s", e)
        return []


def _find_result(results: list, name: str, artist: str, album: str) -> dict | None:
    if not results:
        return None
    name_l   = name.lower()
    artist_l = artist.lower()
    album_l  = album.lower()
    # Best: track + album + artist all match
    exact = next(
        (r for r in results
         if album_l  in r.get("collectionName", "").lower()
         and name_l  in r.get("trackName", "").lower()
         and artist_l in r.get("artistName", "").lower()),
        None,
    )
    if exact:
        return exact
    # Fallback: track + artist match (ignore album variant)
    return next(
        (r for r in results
         if name_l   in r.get("trackName", "").lower()
         and artist_l in r.get("artistName", "").lower()),
        None,
    )


def fetch_extras(persistent_id: str, name: str, artist: str, album: str) -> dict:
    cached = _cache_get(persistent_id)
    if cached:
        return cached

    results = _itunes_search(name, artist, album)
    r = _find_result(results, name, artist, album)

    # Retry without "(Deluxe Edition)" etc. if no result
    if not r and "(" in album:
        clean_album = album[:album.rfind("(")].strip()
        results = _itunes_search(name, artist, clean_album)
        r = _find_result(results, name, artist, clean_album)

    extras = {}
    if r:
        extras["artworkUrl"]       = r.get("artworkUrl100", "").replace("100x100bb", "600x600bb")
        extras["artistViewUrl"]    = r.get("artistViewUrl")
        extras["collectionViewUrl"]= r.get("collectionViewUrl")
        extras["trackViewUrl"]     = r.get("trackViewUrl")

    _cache_set(persistent_id, extras)
    return extras


# ── Windows Media Session ──────────────────────────────────────────────────────

def _is_apple_music(source: str) -> bool:
    s = source.lower()
    return any(k in s for k in ["applemusic", "apple music", "itunes"])


async def _get_track() -> dict | None:
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

        # Use title+artist as persistent ID (Windows doesn't expose one)
        persistent_id = f"{title}|{artist}|{album}"
        duration = None
        try:
            tl = pb.playback_rate  # not duration; get from props if available
        except Exception:
            pass

        return {
            "persistent_id": persistent_id,
            "title":   title,
            "artist":  artist,
            "album":   album,
            "playing": status == PlaybackStatus.PLAYING,
            "paused":  status == PlaybackStatus.PAUSED,
        }

    return None


# ── Presence builder ───────────────────────────────────────────────────────────

def _make_activity(track: dict) -> dict:
    extras = fetch_extras(
        track["persistent_id"],
        track["title"],
        track["artist"],
        track["album"],
    )

    kwargs: dict = {
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
    spotify_q = urllib.parse.quote(f'artist:{track["artist"]} track:{track["title"]}')
    spotify_url = f"https://open.spotify.com/search/{spotify_q}?si"
    if len(spotify_url) <= 512:
        buttons.append({"label": "Search on Spotify", "url": spotify_url})
    if buttons:
        kwargs["buttons"] = buttons[:2]

    return kwargs


# ── Main loop ──────────────────────────────────────────────────────────────────

def main():
    log.info("Apple Music Rich Presence (Windows) starting…")

    rpc: Presence | None = None
    last_id: str | None = None

    while True:
        if rpc is None:
            try:
                rpc = Presence(CLIENT_ID)
                rpc.connect()
                log.info("Connected to Discord RPC.")
            except InvalidPipe:
                log.warning("Discord not running — retrying in %ds…", POLL_INTERVAL)
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
                    reason = "paused" if (track and track["paused"]) else "stopped"
                    log.info("Cleared (%s).", reason)
                    last_id = None
            else:
                pid = track["persistent_id"]
                if pid != last_id:
                    kwargs = _make_activity(track)
                    rpc.update(**kwargs)
                    log.info("▶  %s — %s", track["title"], track["artist"] or "Unknown")
                    last_id = pid

        except InvalidPipe:
            log.warning("Lost Discord connection — reconnecting…")
            rpc = None
            last_id = None
        except Exception as e:
            log.error("Presence update error: %s", e)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped.")
        sys.exit(0)
