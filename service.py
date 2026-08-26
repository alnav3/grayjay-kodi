# -*- coding: utf-8 -*-
"""Background service: source auto-updates + playback tracking.

Kodi starts this (xbmc.service in addon.xml) at boot and keeps it running. It
checks for source updates shortly after startup and then once per configured
interval, persisting the last-run time so a restart doesn't re-check needlessly.
Honors the `auto_update` / `update_interval_hours` settings and aborts promptly
when Kodi shuts down.

It also hosts the player monitor that records watch history / On Deck resume
points and autoplays "Up next" — the plugin process that resolves a stream
exits immediately, so only this long-lived service can watch the player.
"""
import json
import os
import time

import xbmc

from resources.lib.kodiutils import log, profile_path
from resources.lib.sources import updates


_STATE = os.path.join(profile_path(), "update_state.json")
_STARTUP_DELAY = 120          # let the box settle before hitting the network
# Short tick: the player monitor polls playback position each pass (Kodi
# reports no position once playback has stopped, so it must be sampled live).
_TICK = 5
# How often the service refreshes the subscription feed cache. Pulling
# `getChannelContents` for 150+ subs is slow (tens of seconds), so we don't
# want to do it on every plugin open; the cache exists so the listing is
# instant. We also do one immediate refresh shortly after startup so a
# fresh install / cleared cache doesn't show "Loading…" for the first half
# hour.
_SUBFEED_INTERVAL = 1800       # 30 minutes
_SUBFEED_FIRST_DELAY = 180     # first refresh ~3 min after boot


def _last_run():
    try:
        with open(_STATE, "r", encoding="utf-8") as fh:
            return float(json.load(fh).get("last_run", 0))
    except (IOError, OSError, ValueError):
        return 0.0


def _mark_run(ts):
    try:
        with open(_STATE, "w", encoding="utf-8") as fh:
            json.dump({"last_run": ts}, fh)
    except (IOError, OSError):
        pass


def _run_check():
    if not updates.auto_update_enabled():
        return
    log("service: checking for source updates", "info")
    try:
        applied, _checked = updates.update_all()
        log("service: %d source(s) updated" % len(applied), "info")
    except Exception as exc:
        log("service: update run failed: %s" % exc, "error")


def _start_manifest_server():
    """Serve DASH manifests over loopback HTTP for inputstream.adaptive
    (it won't load a local-file manifest). Best-effort; playback falls back to a
    muxed stream if this can't start."""
    try:
        from resources.lib.playback import manifest_server
        cache = os.path.join(profile_path(), "cache")
        server, port = manifest_server.start(cache, profile_path())
        log("service: manifest server on 127.0.0.1:%d" % port, "info")
        return server
    except Exception as exc:
        log("service: manifest server failed to start: %s" % exc, "warning")
        return None


def _refresh_sub_feed():
    """Rebuild the on-disk aggregated subscription feed.

    Delegates to sub_feed_cache.refresh_now() so the manifest server can
    trigger the same routine over HTTP without us having to expose it
    twice or worry about circular imports."""
    try:
        from resources.lib.sources.sub_feed_cache import refresh_now
    except Exception as exc:
        log("service: subfeed refresh import failed: %s" % exc, "warning")
        return
    n = refresh_now()
    log("service: subfeed refresh done (%d item(s))" % n, "info")


def main():
    monitor = xbmc.Monitor()
    log("service started", "info")
    manifest_srv = _start_manifest_server()
    try:
        from resources.lib.playback.monitor import PlayerMonitor
        player = PlayerMonitor()
    except Exception as exc:
        log("service: player monitor unavailable: %s" % exc, "warning")
        player = None
    started = time.time()
    first_subfeed_done = False
    last_subfeed_run = 0.0

    while not monitor.waitForAbort(_TICK):
        if player:
            try:
                player.tick()
            except Exception as exc:
                log("service: player tick failed: %s" % exc, "warning")
        try:
            from resources.lib.playback import ump_sessions
            ump_sessions.evict_idle()
        except Exception as exc:
            log("service: ump session eviction failed: %s" % exc, "warning")
        try:
            from resources.lib.playback import session_registry
            session_registry.evict_idle()
        except Exception as exc:
            log("service: session registry eviction failed: %s" % exc, "warning")
        # Stagger the first update check so we don't compete with Kodi boot.
        if time.time() - started < _STARTUP_DELAY:
            continue
        interval = updates.update_interval_hours() * 3600
        if updates.auto_update_enabled() and (time.time() - _last_run()) >= interval:
            _run_check()
            _mark_run(time.time())

        # Subscription feed cache: one refresh ~3 min after boot, then
        # every _SUBFEED_INTERVAL seconds. The plugin reads from the
        # cache synchronously, so this is what makes the listing load
        # instantly for the user.
        now = time.time()
        subfeed_due = first_subfeed_done and (now - last_subfeed_run) >= _SUBFEED_INTERVAL
        subfeed_first = (not first_subfeed_done
                         and (now - started) >= _SUBFEED_FIRST_DELAY)
        if subfeed_first or subfeed_due:
            try:
                _refresh_sub_feed()
            except Exception as exc:
                log("service: subfeed refresh failed: %s" % exc, "warning")
            first_subfeed_done = True
            last_subfeed_run = now
    if manifest_srv:
        manifest_srv.shutdown()
    try:
        from resources.lib.playback import session_registry
        session_registry.evict_idle(max_age=0)  # kill every warm bridge on shutdown
    except Exception as exc:
        log("service: session registry shutdown cleanup failed: %s" % exc, "warning")
    log("service stopped", "info")


if __name__ == "__main__":
    main()
