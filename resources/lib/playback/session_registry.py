# -*- coding: utf-8 -*-
"""Registry of long-lived, warm PluginBridges -- one per source, shared across
every browse/play call for as long as the background service runs.

Kodi's `plugin.video.*` process is spawned fresh for every single navigation
or play action and exits right after (see router.py's docstring). For a
YouTube-shaped source that means re-running BotGuard attestation + session-
client init -- tens of seconds of network round trips with no connection
reuse -- from scratch on every video, because nothing survives between
invocations except a best-effort disk-cached saveState() (resources/lib/
sources/plugin_state.py) that only ever gets written if that same slow,
fragile cold-start happens to finish without throwing first.

This module keeps one real PluginBridge (and its live qjs subprocess, warm
BotGuard token, initialized session client) alive in the persistent
background service's process instead, exactly like ump_sessions.py already
does for individual UMP playbacks -- except keyed by source_id and long-lived
across many videos, not per-playback. The short-lived router.py process talks
to it over the same loopback HTTP server manifest_server.py already runs
(see its `/session/call` route), so the expensive setup happens once per
service lifetime (matching how the native Grayjay app -- one continuously
running process -- behaves) instead of once per video.
"""
import threading
import time

from ..kodiutils import log

_ENTRIES = {}
_REGISTRY_LOCK = threading.Lock()  # guards _ENTRIES itself, not a given bridge
_IDLE_TIMEOUT_S = 2 * 3600  # free memory/qjs subprocess for sources not in use
# bridge.call() blocks synchronously on the qjs subprocess pipe with no
# built-in cancellation (see ump_sessions.py's identical concern). Bound every
# call so a genuine hang wedges one source's bridge, never the HTTP handler
# thread indefinitely. Generous: a cold enable() can legitimately take ~45s,
# and getContentDetails has its own internal async_timeout (default 90s).
_CALL_TIMEOUT_S = 150


def _call_with_timeout(bridge, method, args, timeout_s=_CALL_TIMEOUT_S):
    result = {}

    def run():
        try:
            result["value"] = bridge.call(method, args)
        except Exception as exc:
            result["error"] = exc

    t = threading.Thread(target=run, name="session-call-%s" % method, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise TimeoutError("%s timed out after %ss" % (method, timeout_s))
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _get_or_create_entry(source_id):
    with _REGISTRY_LOCK:
        entry = _ENTRIES.get(source_id)
        if entry is None:
            entry = _ENTRIES[source_id] = {
                "bridge": None,
                "lock": threading.Lock(),
                "last_used": time.time(),
            }
        return entry


def _drop(source_id, entry):
    with _REGISTRY_LOCK:
        if _ENTRIES.get(source_id) is entry:
            del _ENTRIES[source_id]
    bridge = entry.get("bridge")
    if bridge is not None:
        try:
            bridge.close()
        except Exception:
            pass


def call(source_id, method, args):
    """Run `method(*args)` on this source's warm bridge, creating it (paying
    the one-time cold-start cost) if this is the first call since the service
    started. Returns {"result": ..., "stream_harvest": [...],
    "muxed_harvest": [...]}, or raises on failure (source not found, cold
    start failed, or the call itself errored/timed out) -- the caller (see
    manifest_server.py's /session/call) turns that into an HTTP error so
    router.py can fall back to a local, ephemeral bridge.
    """
    entry = _get_or_create_entry(source_id)
    # Serialize access to this source's bridge -- the qjs subprocess pipe
    # protocol is not concurrency-safe (see jsengine.py's dispatch loop) --
    # while letting different sources' bridges run fully in parallel.
    with entry["lock"]:
        entry["last_used"] = time.time()
        if entry["bridge"] is None:
            from ..engine.bridge import create_enabled_bridge
            bridge = create_enabled_bridge(source_id)
            if bridge is None:
                _drop(source_id, entry)
                raise LookupError("source not found: %s" % source_id)
            entry["bridge"] = bridge
            log("session: warmed up bridge for %s" % source_id, "info")
        bridge = entry["bridge"]
        try:
            value = _call_with_timeout(bridge, method, args)
        except Exception:
            # Anything from a timeout to a JS-side crash leaves the bridge in
            # an unknown state -- drop it so the next call gets a fresh one
            # rather than repeatedly hitting a wedged subprocess.
            log("session: %s.%s failed, dropping bridge" % (source_id, method), "warning")
            _drop(source_id, entry)
            raise
        try:
            from ..sources import plugin_state
            state = bridge.save_state()
            if state:
                plugin_state.save(bridge.config, state)
        except Exception as exc:
            log("session: persisting state failed: %s" % exc, "debug")
        return {
            "result": value,
            "stream_harvest": bridge.harvested_streams(),
            "muxed_harvest": bridge.harvested_muxed(),
            "caption_harvest": bridge.harvested_captions(),
        }


def evict_idle(max_age=_IDLE_TIMEOUT_S):
    """Close and drop bridges unused for max_age seconds. Called from the
    background service's tick loop, same as ump_sessions.evict_idle."""
    now = time.time()
    with _REGISTRY_LOCK:
        stale = [sid for sid, e in _ENTRIES.items() if now - e["last_used"] > max_age]
    for sid in stale:
        entry = _ENTRIES.get(sid)
        if entry is not None:
            _drop(sid, entry)
    if stale:
        log("session: evicted %d idle source bridge(s)" % len(stale), "debug")
