# -*- coding: utf-8 -*-
"""Watch state: History, On Deck (partially watched), and play queues.

Three small persistence pieces that together give continue-watching behaviour:

* watch_state.json — one entry per tracked video: last playback position,
  duration, and a play count. "History" is entries with a play count;
  "On Deck" is entries with a meaningful resume position.
* queues/<qid>.json — a snapshot of the playable items of the directory the
  user started playback from, so the player can offer and play "Up next".
* now_playing.json — the router's handoff to the background service: written
  when a stream resolves (the plugin process exits right after
  setResolvedUrl, so it can't watch the player itself), claimed by the
  service's player monitor when playback actually starts.
"""
import hashlib
import json
import os
import re
import time

from .kodiutils import profile_path, log

WATCHED_FRACTION = 0.9      # ≥90% played counts as watched
MIN_TRACK_SECONDS = 30      # dips shorter than this are noise, not "On Deck"
MAX_ENTRIES = 500           # oldest tracked videos age out past this
QUEUE_KEEP = 20             # queue snapshots kept before pruning
NOW_PLAYING_MAX_AGE = 300   # resolve→playback-start handoff window (seconds)

_QID_RE = re.compile(r"^[0-9a-f]{8,32}$")


# -- watch state ------------------------------------------------------------
def _state_path():
    return os.path.join(profile_path(), "watch_state.json")


def _load():
    try:
        with open(_state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (IOError, OSError, ValueError):
        return []


def _save(entries):
    entries.sort(key=lambda e: e.get("updated") or 0, reverse=True)
    del entries[MAX_ENTRIES:]
    try:
        with open(_state_path(), "w", encoding="utf-8") as fh:
            json.dump(entries, fh, indent=2)
    except (IOError, OSError) as exc:
        log("failed to write watch state: %s" % exc, "warning")


def all_entries():
    """Every tracked entry, most recently updated first."""
    return _load()


def get_entry(source_id, url):
    for e in _load():
        if e.get("source") == source_id and e.get("url") == url:
            return e
    return None


def _upsert(entries, source_id, url, name, thumbnail, duration):
    for e in entries:
        if e.get("source") == source_id and e.get("url") == url:
            if name:
                e["name"] = name
            if thumbnail:
                e["thumbnail"] = thumbnail
            if duration:
                e["duration"] = duration
            return e
    e = {"source": source_id, "url": url, "name": name or url,
         "thumbnail": thumbnail or "", "duration": duration or 0,
         "position": 0, "playcount": 0, "updated": 0}
    entries.append(e)
    return e


def record_progress(source_id, url, name="", thumbnail="", position=0, duration=0):
    """Remember how far into a video playback got. Positions inside the first
    MIN_TRACK_SECONDS are ignored (the user just browsed away); positions past
    WATCHED_FRACTION mark the video watched instead of leaving it On Deck."""
    if not source_id or not url:
        return False
    try:
        position = float(position or 0)
        duration = float(duration or 0)
    except (TypeError, ValueError):
        return False
    if position < MIN_TRACK_SECONDS:
        return False
    if duration > 0 and (position / duration) >= WATCHED_FRACTION:
        return mark_watched(source_id, url, name, thumbnail, duration)
    entries = _load()
    e = _upsert(entries, source_id, url, name, thumbnail, duration)
    e["position"] = position
    e["updated"] = time.time()
    _save(entries)
    return True


def mark_watched(source_id, url, name="", thumbnail="", duration=0):
    """Move a video to History: bump its play count, drop the resume point."""
    if not source_id or not url:
        return False
    entries = _load()
    e = _upsert(entries, source_id, url, name, thumbnail, duration)
    e["playcount"] = int(e.get("playcount") or 0) + 1
    e["position"] = 0
    e["updated"] = time.time()
    _save(entries)
    return True


def remove(source_id, url):
    entries = _load()
    kept = [e for e in entries
            if not (e.get("source") == source_id and e.get("url") == url)]
    if len(kept) == len(entries):
        return False
    _save(kept)
    return True


def clear_history():
    _save([e for e in _load() if not int(e.get("playcount") or 0)])


def list_history():
    """Watched videos, most recently watched first."""
    out = [e for e in _load() if int(e.get("playcount") or 0) > 0]
    out.sort(key=lambda e: e.get("updated") or 0, reverse=True)
    return out


def list_on_deck():
    """Partially watched videos (resume point saved), most recent first.
    A watched video being rewatched appears here too — that's the resume."""
    out = [e for e in _load()
           if float(e.get("position") or 0) >= MIN_TRACK_SECONDS]
    out.sort(key=lambda e: e.get("updated") or 0, reverse=True)
    return out


# -- play queues ("Up next") --------------------------------------------------
def _queue_dir():
    d = os.path.join(profile_path(), "queues")
    if not os.path.isdir(d):
        os.makedirs(d)
    return d


def make_queue_id(seed):
    """Stable id for a directory listing: same listing → same snapshot file."""
    return hashlib.md5(seed.encode("utf-8")).hexdigest()[:16]


def save_queue(qid, items):
    """Snapshot a listing's playable items:
    [{"play": <plugin url>, "source", "url", "name", "thumbnail", "duration"}]"""
    if not _QID_RE.match(qid or "") or not items:
        return False
    path = os.path.join(_queue_dir(), "%s.json" % qid)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"saved": time.time(), "items": items}, fh)
    except (IOError, OSError) as exc:
        log("failed to write play queue: %s" % exc, "warning")
        return False
    _prune_queues()
    return True


def _prune_queues():
    d = _queue_dir()
    try:
        files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".json")]
        files.sort(key=os.path.getmtime, reverse=True)
        for f in files[QUEUE_KEEP:]:
            os.remove(f)
    except OSError:
        pass


def queue_item(qid, idx):
    """items[idx] of a stored queue, or None."""
    if not _QID_RE.match(qid or ""):
        return None
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return None
    try:
        with open(os.path.join(_queue_dir(), "%s.json" % qid),
                  "r", encoding="utf-8") as fh:
            items = json.load(fh).get("items") or []
    except (IOError, OSError, ValueError):
        return None
    if 0 <= idx < len(items):
        return items[idx]
    return None


# -- now-playing handoff ------------------------------------------------------
def _now_playing_path():
    return os.path.join(profile_path(), "now_playing.json")


def set_now_playing(info):
    """Router → service note about the stream that is about to start."""
    payload = dict(info)
    payload["at"] = time.time()
    try:
        with open(_now_playing_path(), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except (IOError, OSError) as exc:
        log("failed to write now-playing handoff: %s" % exc, "warning")


def claim_now_playing(max_age=NOW_PLAYING_MAX_AGE):
    """Read-and-delete the pending handoff, if fresh enough to belong to the
    playback that just started."""
    p = _now_playing_path()
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (IOError, OSError, ValueError):
        return None
    try:
        os.remove(p)
    except OSError:
        pass
    try:
        age = time.time() - float(data.get("at") or 0)
    except (TypeError, ValueError):
        return None
    return data if 0 <= age <= max_age else None
