# -*- coding: utf-8 -*-
"""Persist a source's `saveState()` output across sessions.

Grayjay plugins can serialise expensive-to-build state via `source.saveState()`
and receive it back as the third argument to `enable(config, settings,
savedState)`. The YouTube plugin uses this for its innertube context / session
client — rebuilding that from scratch (BotGuard attestation included) is the
bulk of the "select a video → wait up to a minute" delay, because every Kodi
plugin invocation is a fresh process.

The stored blob is keyed to the exact plugin script (sha256), so a source
update automatically invalidates it, and aged out after a few hours since the
sessions it caches (tokens, pre-signed URLs) expire server-side anyway.
"""
import hashlib
import json
import os
import time

# Session tokens / pre-signed URLs the state caches go stale server-side;
# YouTube's are good for roughly six hours.
STATE_TTL_SECONDS = 6 * 3600
MAX_STATE_BYTES = 256 * 1024


def _path(config):
    return os.path.join(config.base_dir, "state.json")


def _script_hash(config):
    try:
        with open(config.script_path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except (IOError, OSError):
        return ""


def load(config):
    """The stored state string for this source, or '' when absent/stale."""
    try:
        with open(_path(config), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (IOError, OSError, ValueError):
        return ""
    if data.get("script_sha256") != _script_hash(config):
        return ""
    if (time.time() - float(data.get("saved_at") or 0)) > STATE_TTL_SECONDS:
        return ""
    state = data.get("state")
    return state if isinstance(state, str) else ""


def save(config, state):
    """Persist a saveState() string; returns True when written."""
    if not state or not isinstance(state, str):
        return False
    if len(state.encode("utf-8")) > MAX_STATE_BYTES:
        return False
    payload = {
        "script_sha256": _script_hash(config),
        "saved_at": time.time(),
        "state": state,
    }
    try:
        with open(_path(config), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return True
    except (IOError, OSError):
        return False


def clear(config):
    try:
        os.remove(_path(config))
    except OSError:
        pass
