# -*- coding: utf-8 -*-
"""SponsorBlock segment fetching and category-aware skip logic.

Fetches community-submitted sponsor segments from the SponsorBlock API
(https://sponsor.ajay.app) for a given YouTube video ID, filters them against
the per-category plugin settings (sponsorBlockCat_* dropdowns: 0=No skip,
1=Manual, 2=Automatic), and exposes helper methods for the player monitor to
decide whether to skip or prompt at a given playback position.

The SponsorBlock API endpoint is public, rate-limited, and requires only the
video ID — no authentication.  We cache the fetched segments per video ID in
memory so repeated ticks don't re-fetch.
"""
import json
import time

from ..kodiutils import log

_API_BASE = "https://sponsor.ajay.app/api"
_SEGMENTS_URL = _API_BASE + "/skipSegments"
# How long to cache fetched segments (seconds).  30 min covers a typical
# long-form video plus some rewatching.
_CACHE_TTL = 1800

# SponsorBlock setting variable prefix → SponsorBlock API category name.
# The plugin declares dropdowns like sponsorBlockCat_Sponsor; we map the
# human-friendly suffix to the API's lowercase slug.
_CATEGORY_MAP = {
    "Sponsor":            "sponsor",
    "Self-Promotion":     "selfpromo",
    "Interaction":        "interaction",
    "Intro":              "intro",
    "Outro":              "outro",
    "Preview":            "preview",
    "Music Off-topic":    "music_offtopic",
    "Filler":             "filler",
    "Highlight":          "poi",
    "Chapter":            "chapter",
}

# Action constants returned by segment_action.
ACTION_NONE = 0
ACTION_MANUAL = 1
ACTION_SKIP = 2

_cache = {}  # video_id -> {"segments": [...], "ts": float}


def _resolve_video_id(content_url):
    """Best-effort extraction of a YouTube video ID from a content URL or ID.

    The YouTube plugin passes contentUrl values that are either bare video IDs
    (11 chars) or full URLs containing the ID.  Returns None when we can't
    tell."""
    if not content_url:
        return None
    # Bare video IDs are exactly 11 chars of [A-Za-z0-9_-].
    if len(content_url) == 11 and all(
        c.isalnum() or c in "-_" for c in content_url
    ):
        return content_url
    # Look for v=<id> query param or /watch?v=<id> path.
    for marker in ("v=", "/v/"):
        idx = content_url.find(marker)
        if idx >= 0:
            start = idx + len(marker)
            candidate = content_url[start:start + 11]
            if len(candidate) == 11:
                return candidate
    # Last resort: YouTube short URLs use /<id>.
    parts = content_url.rstrip("/").split("/")
    if parts:
        tail = parts[-1]
        if len(tail) == 11 and all(c.isalnum() or c in "-_" for c in tail):
            return tail
    return None


def _categories_from_settings(settings):
    """Return a dict of {sblock_api_category: action} from plugin settings.

    `settings` is the merged {variable: value} dict from plugin_settings.load().
    Dropdown values: 0 = No skip, 1 = Manual, 2 = Automatic."""
    cats = {}
    for var, val in settings.items():
        if not var.startswith("sponsorBlockCat_"):
            continue
        suffix = var[len("sponsorBlockCat_"):]
        api_cat = _CATEGORY_MAP.get(suffix)
        if api_cat is None:
            continue
        try:
            action = int(val)
        except (TypeError, ValueError):
            action = 0
        if action > 0:
            cats[api_cat] = action
    return cats


def fetch_segments(video_id, categories=None):
    """Fetch SponsorBlock segments for `video_id`.

    Returns a list of segment dicts from the API, or [].  Results are cached
    in memory for _CACHE_TTL seconds.  If `categories` is provided, only
    segments whose category is in the set are returned (reduces payload and
    avoids storing irrelevant data)."""
    if not video_id:
        return []
    cached = _cache.get(video_id)
    if cached and (time.time() - cached["ts"]) < _CACHE_TTL:
        segs = cached["segments"]
        if categories is not None:
            segs = [s for s in segs if s.get("category") in categories]
        return segs

    try:
        import urllib.request
        import urllib.error
        url = "%s?videoID=%s" % (_SEGMENTS_URL, video_id)
        req = urllib.request.Request(url, headers={
            "User-Agent": "grayjay-kodi/1.0",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 404 = no segments for this video; not an error, just empty.
        if exc.code == 404:
            _cache[video_id] = {"segments": [], "ts": time.time()}
            return []
        log("sponsorblock: HTTP %d for %s" % (exc.code, video_id), "debug")
        return []
    except Exception as exc:
        log("sponsorblock: fetch failed for %s: %s" % (video_id, exc), "debug")
        return []

    # The API returns a list of {"uuid": ..., "category": ..., "actionType": ...,
    # "segment": [start, end], ...}
    segments = []
    for entry in (data or []):
        seg = entry.get("segment") or []
        if len(seg) < 2:
            continue
        segments.append({
            "uuid": entry.get("uuid", ""),
            "category": entry.get("category", ""),
            "action_type": entry.get("actionType", "skip"),
            "start": float(seg[0]),
            "end": float(seg[1]),
        })

    _cache[video_id] = {"segments": segments, "ts": time.time()}

    if categories is not None:
        segments = [s for s in segments if s.get("category") in categories]
    return segments


def segment_action(segment, category_settings):
    """Return ACTION_NONE / ACTION_MANUAL / ACTION_SKIP for a segment,
    based on the user's per-category settings."""
    cat = segment.get("category", "")
    action = category_settings.get(cat, ACTION_NONE)
    # The SponsorBlock API sometimes returns actionType "mute" or "full" in
    # addition to "skip".  Honour the user's category choice regardless of
    # actionType — if they set "Automatic" for a category, we skip/mute
    # every segment in it.
    return action


def check_position(position, segments, category_settings):
    """Check whether the current playback `position` (seconds) falls inside
    any SponsorBlock segment.

    Returns (action, segment) where action is ACTION_NONE / ACTION_MANUAL /
    ACTION_SKIP and segment is the matching dict (or None).
    """
    for seg in segments:
        if seg["start"] <= position <= seg["end"]:
            act = segment_action(seg, category_settings)
            if act != ACTION_NONE:
                return act, seg
    return ACTION_NONE, None


def invalidate_cache(video_id):
    """Drop cached segments for a video (e.g. on playback stop)."""
    _cache.pop(video_id, None)
