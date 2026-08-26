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

from ..kodiutils import log, profile_path


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


def refresh_now():
    """Rebuild the aggregated cache from the network.

    Lives here (not in service.py) so the manifest server's HTTP handler
    can trigger it without a circular import — the handler runs in the
    service process but can't import `service` mid-load. Returns the
    number of items written to the top-level cache."""
    import json
    try:
        from . import subscriptions as subs, groups as grp
    except Exception as exc:
        log("subfeed refresh: imports failed: %s" % exc, "warning")
        return 0
    try:
        from ..engine.bridge import create_enabled_bridge
    except Exception as exc:
        log("subfeed refresh: bridge import failed: %s" % exc, "warning")
        return 0

    def _normalize(raw):
        if raw is None:
            return []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                return []
        if isinstance(raw, dict):
            return raw.get("results") or raw.get("items") or []
        if isinstance(raw, list):
            return raw
        return []

    feed_subs = subs.list_subscriptions()
    if not feed_subs:
        save("__all__", [])
        return 0

    collected = []
    for s in feed_subs:
        try:
            bridge = create_enabled_bridge(s["source"])
            if bridge is None:
                continue
            try:
                raw = bridge.call("getChannelContents",
                                  [s["url"], None, None, [], None])
            finally:
                try:
                    bridge.close()
                except Exception:
                    pass
            for v in _normalize(raw):
                if isinstance(v, dict):
                    collected.append((s["source"], v))
        except Exception as exc:
            log("subfeed channel %s failed: %s" % (s.get("url"), exc),
                "warning")
            continue
    collected.sort(key=lambda sv: (sv[1].get("datetime") or 0), reverse=True)
    save("__all__", collected)

    for g in grp.list_groups():
        members = {(m.get("source"), m.get("url"))
                   for m in g.get("members", [])}
        if not members:
            save(g["id"], [])
            continue
        filtered = [(src, v) for src, v in collected
                    if (src, v.get("url")) in members]
        filtered.sort(key=lambda sv: (sv[1].get("datetime") or 0), reverse=True)
        save(g["id"], filtered)

    return len(collected)
