# -*- coding: utf-8 -*-
"""Background service: source auto-updates + playback tracking + LAN sync.

Kodi starts this (xbmc.service in addon.xml) at boot and keeps it running. It
checks for source updates shortly after startup and then once per configured
interval, persisting the last-run time so a restart doesn't re-check needlessly.
Honors the `auto_update` / `update_interval_hours` settings and aborts promptly
when Kodi shuts down.

It also hosts the player monitor that records watch history / On Deck resume
points and autoplays "Up next" — the plugin process that resolves a stream
exits immediately, so only this long-lived service can watch the player.

When `sync_enabled` is true, also starts the SyncService listener (LAN-only
TCP) and a reconnect loop that tries to reach previously paired devices
every `sync_reconnect_minutes`. Incoming connections are accepted on the
configured `sync_port` and trigger a one-shot sync of subscriptions + groups.
"""
import json
import os
import threading
import time

import xbmc

from resources.lib.kodiutils import log, profile_path, get_setting
from resources.lib.sources import updates


_STATE = os.path.join(profile_path(), "update_state.json")
_STARTUP_DELAY = 120          # let the box settle before hitting the network
# Short tick: the player monitor polls playback position each pass (Kodi
# reports no position once playback has stopped, so it must be sampled live).
_TICK = 5
_SYNC_RECONNECT_INTERVAL = 300  # 5 minutes


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


def _start_sync_service():
    if get_setting("sync_enabled", "false") != "true":
        return None
    try:
        from resources.lib.sync.router_actions import get_service
        svc = get_service()
        # Register a callback that runs the full sync once a connection is
        # authorized, so incoming connections don't sit idle.
        from resources.lib.sync.router_actions import run_full_sync
        def _on_auth(sess):
            try:
                run_full_sync(sess)
            except Exception as exc:
                log("service: sync-on-auth failed: %s" % exc, "error")
        svc.on_authorized(_on_auth)
        return svc
    except Exception as exc:
        log("service: sync startup failed: %s" % exc, "error")
        return None


def _reconnect_loop(svc, monitor):
    """Background: try to reach each known peer every few minutes."""
    next_attempt = 0
    while not monitor.abortRequested():
        now = time.time()
        if now >= next_attempt:
            next_attempt = now + _SYNC_RECONNECT_INTERVAL
            try:
                devs = svc.list_authorized()
                for pk, info in list(devs.items()):
                    addr = info.get("last_address")
                    if not addr:
                        continue
                    if pk in svc._sessions:
                        continue  # already connected
                    log("service: reconnecting to %s @ %s" % (pk[:8], addr), "info")
                    def _do_connect(addr=addr, pk=pk):
                        try:
                            svc.connect_and_pair({
                                "public_key": pk,
                                "addresses": [addr],
                                "port": svc.listener_port,
                                "pairing_code": "",  # not needed for re-auth
                            })
                        except Exception as exc:
                            log("service: reconnect to %s failed: %s" % (pk[:8], exc), "debug")
                    threading.Thread(target=_do_connect, daemon=True).start()
            except Exception as exc:
                log("service: reconnect loop failed: %s" % exc, "debug")
        if monitor.waitForAbort(30):
            return


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
    sync_svc = _start_sync_service()
    if sync_svc:
        log("service: sync listener active on port %d" % sync_svc.listener_port, "info")
        threading.Thread(target=_reconnect_loop, args=(sync_svc, monitor),
                         name="sync-reconnect", daemon=True).start()
    started = time.time()

    while not monitor.waitForAbort(_TICK):
        if player:
            try:
                player.tick()
            except Exception as exc:
                log("service: player tick failed: %s" % exc, "warning")
        # Stagger the first update check so we don't compete with Kodi boot.
        if time.time() - started < _STARTUP_DELAY:
            continue
        interval = updates.update_interval_hours() * 3600
        if updates.auto_update_enabled() and (time.time() - _last_run()) >= interval:
            _run_check()
            _mark_run(time.time())
    if manifest_srv:
        manifest_srv.shutdown()
    if sync_svc:
        sync_svc.stop()
    log("service stopped", "info")


if __name__ == "__main__":
    main()
