# -*- coding: utf-8 -*-
"""Disk-backed cache for the aggregated subscription feed.

The Subscriptions listing merges 150+ `getChannelContents` calls into one
newest-first list. On a fresh Kodi start that takes many seconds; without
a cache the user stares at a blank directory until it's done. This module
persists the last successful merge per group (keyed by group id, or
"__all__" for the un-grouped view) so the listing can render instantly
from disk while a background thread refreshes from the network.

Stale-on-write, not stale-on-load: we save *after* every successful
refresh and serve the cached version regardless of age. Items older than
the cache window fall off naturally — they'll be replaced or pushed down
once the network refresh returns.
"""
import json
import os
import tempfile

from ..kodiutils import profile_path


def _cache_path():
    return os.path.join(profile_path(), "sub_feed_cache.json")


def _read():
    try:
        with open(_cache_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, dict):
                return data
    except (IOError, OSError, ValueError):
        pass
    return {}


def _write(data):
    path = _cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except (IOError, OSError):
        pass
    fd, tmp = tempfile.mkstemp(prefix=".subfeed.", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def load(group_key):
    """Return the cached feed list for this group, or [] if none."""
    if not group_key:
        group_key = "__all__"
    return _read().get(group_key) or []


def save(group_key, collected):
    """Persist the freshly-aggregated feed list for this group."""
    if not group_key:
        group_key = "__all__"
    data = _read()
    data[group_key] = collected
    _write(data)


def clear(group_key=None):
    """Drop one group's cache entry, or everything if group_key is None."""
    if group_key is None:
        _write({})
        return
    data = _read()
    data.pop(group_key, None)
    _write(data)
