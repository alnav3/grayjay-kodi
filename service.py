# -*- coding: utf-8 -*-
"""Background service: periodically auto-update installed Grayjay sources.

Kodi starts this (xbmc.service in addon.xml) at boot and keeps it running. It
checks for source updates shortly after startup and then once per configured
interval, persisting the last-run time so a restart doesn't re-check needlessly.
Honors the `auto_update` / `update_interval_hours` settings and aborts promptly
when Kodi shuts down.
"""
import json
import os
import time

import xbmc

from resources.lib.kodiutils import log, profile_path
from resources.lib.sources import updates


_STATE = os.path.join(profile_path(), "update_state.json")
_STARTUP_DELAY = 120          # let the box settle before hitting the network
_TICK = 60                    # how often the loop re-evaluates (seconds)


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
        applied, _checked = updates.update_all(notify_summary=True)
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


def main():
    monitor = xbmc.Monitor()
    log("service started", "info")
    manifest_srv = _start_manifest_server()
    # Stagger the first check after startup so we don't compete with Kodi boot.
    if monitor.waitForAbort(_STARTUP_DELAY):
        if manifest_srv:
            manifest_srv.shutdown()
        return

    while not monitor.abortRequested():
        interval = updates.update_interval_hours() * 3600
        if updates.auto_update_enabled() and (time.time() - _last_run()) >= interval:
            _run_check()
            _mark_run(time.time())
        if monitor.waitForAbort(_TICK):
            break
    if manifest_srv:
        manifest_srv.shutdown()
    log("service stopped", "info")


if __name__ == "__main__":
    main()
