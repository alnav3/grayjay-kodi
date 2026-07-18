# -*- coding: utf-8 -*-
"""Router-side actions for sync: pairing, listing devices, sync now, remove.

These are called by `router.py` from the [ Sync ] submenu and from the
context menu on a paired device's row. Heavy lifting (TCP listener,
authorized-device state, pairing URL generation) lives in `service.py`;
these actions just build the dialogs and orchestrate calls.
"""
import threading
import time

from ..kodiutils import notify, log, get_setting, set_setting
from . import records
from .service import SyncService


_SINGLETON = None
_SINGLETON_LOCK = threading.Lock()


def get_service():
    """Return the singleton SyncService for this addon, creating it lazily.

    The listener is started on first access if `sync_enabled` is true. Service.py
    keeps its own reference so the listener survives between router invocations."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = SyncService()
        if get_setting("sync_enabled", "false") == "true" and not _SINGLETON.is_running:
            _SINGLETON.start()
        return _SINGLETON


def action_root(router):
    """Sync menu: show pairing URL, pair, list devices, sync now, remove."""
    from ..sources import manager
    items = []
    items.append((router.url_for(action="sync_show_pairing_url"), "[ Show pairing URL ]", False, ""))
    items.append((router.url_for(action="sync_pair_url"), "[ Pair with URL… ]", False, ""))
    items.append((router.url_for(action="sync_devices"), "[ Paired devices… ]", True, ""))
    items.append((router.url_for(action="sync_now_all"), "[ Sync now with all ]", False, ""))
    return items


def action_show_pairing_url(_router):
    """Show the grayjay:// pairing URL in a Kodi dialog so the user can copy
    it to the other device."""
    svc = get_service()
    if not svc.is_running:
        if _HAS_KODI:
            import xbmcgui
            xbmcgui.Dialog().ok("Sync", "Sync is disabled. Enable it in Settings first.")
        return
    url = svc.get_pairing_url()
    if not url:
        notify("Sync: no pairing URL available")
        return
    if _HAS_KODI:
        import xbmcgui
        xbmcgui.Dialog().ok("Sync pairing URL",
                            "On the other device, paste this URL into\n"
                            "the 'Pair with URL' menu.\n\n"
                            "Pairing code: %s\n\n%s" % (svc.pairing_code, url))
    notify("Pairing code: %s" % svc.pairing_code)


def action_pair_url(_router):
    """Prompt for a grayjay://sync/<...> URL and connect to that peer."""
    if not _HAS_KODI:
        return
    import xbmcgui
    url = xbmcgui.Dialog().input("Paste grayjay://sync/ URL")
    if not url:
        return
    svc = get_service()
    if not svc.is_running:
        if not xbmcgui.Dialog().yesno("Sync disabled",
                                       "Sync is not enabled. Start it now?"):
            return
        set_setting("sync_enabled", "true")
        svc.start()
    info = svc.parse_pairing_url(url.strip())
    if not info:
        notify("Invalid pairing URL")
        return
    name = info.get("name") or "(unknown)"

    def _after_auth(sess):
        # Send our AUTHORIZED notify.
        id_str = sess.session_id.hex()
        id_bytes = id_str.encode("utf-8")
        from ..sources import manager
        device_name = svc.device_name
        name_bytes = device_name.encode("utf-8")
        payload = bytes([len(id_bytes)]) + id_bytes + bytes([len(name_bytes)]) + name_bytes
        sess.send_notify(0, payload)  # NotifyOpcode.AUTHORIZED
        # Immediately sync subscriptions + groups.
        try:
            run_full_sync(sess)
        except Exception as exc:
            log("initial sync failed: %s" % exc, "error")
        notify("Synced with %s" % name)

    threading.Thread(target=_pair_worker, args=(svc, info, _after_auth),
                     daemon=True).start()


def _pair_worker(svc, info, callback):
    try:
        svc.connect_and_pair(info, post_auth_callback=callback)
    except Exception as exc:
        log("pairing failed: %s" % exc, "error")
        notify("Pairing failed: %s" % exc)


def action_devices(router):
    """List paired devices with sync-now / rename / remove actions."""
    svc = get_service()
    devs = svc.list_authorized()
    if not devs:
        return [(router.url_for(action="sync_root"),
                 "No paired devices yet", False, "")]
    items = []
    for pk, info in sorted(devs.items(), key=lambda kv: kv[1].get("name", "")):
        label = "%s  ·  %s" % (info.get("name") or pk[:8] + "...", info.get("last_address", ""))
        ctx = [
            ("Sync now",
             "RunPlugin(%s)" % router.url_for(action="sync_now_one", peer=pk)),
            ("Rename",
             "RunPlugin(%s)" % router.url_for(action="sync_rename", peer=pk)),
            ("Forget device",
             "RunPlugin(%s)" % router.url_for(action="sync_forget", peer=pk)),
        ]
        items.append((router.url_for(action="sync_devices"), label, True, "", None, ctx))
    return items


def action_now_one(router):
    """Manually sync with one peer."""
    peer = router.args.get("peer")
    if not peer:
        return
    svc = get_service()
    sess = svc._sessions.get(peer)
    if sess is None:
        # Try to reconnect using last known address.
        info = svc.list_authorized().get(peer)
        if not info or not info.get("last_address"):
            notify("No connection to that device")
            return
        notify("Reconnecting to %s..." % (info.get("name") or peer[:8]))

        def _post(sess):
            sess.send_notify(0, _authorized_payload(svc, sess))
            try:
                run_full_sync(sess)
            except Exception as exc:
                log("sync failed: %s" % exc, "error")
            notify("Synced with %s" % (info.get("name") or peer[:8]))

        def _do_connect():
            try:
                fake_info = {
                    "public_key": peer,
                    "addresses": [info["last_address"]],
                    "port": svc.listener_port,
                    "pairing_code": "",
                }
                svc.connect_and_pair(fake_info, post_auth_callback=_post)
            except Exception as exc:
                log("reconnect failed: %s" % exc, "error")
                notify("Reconnect failed: %s" % exc)

        threading.Thread(target=_do_connect, daemon=True).start()
        return
    try:
        run_full_sync(sess)
        notify("Synced")
    except Exception as exc:
        log("sync failed: %s" % exc, "error")
        notify("Sync failed: %s" % exc)


def action_now_all(_router):
    svc = get_service()
    if not svc._sessions:
        notify("No active sync sessions")
        return
    for sess in list(svc._sessions.values()):
        try:
            run_full_sync(sess)
        except Exception as exc:
            log("sync failed: %s" % exc, "error")
    notify("Sync complete")


def action_rename(router):
    if not _HAS_KODI:
        return
    import xbmcgui
    peer = router.args.get("peer")
    svc = get_service()
    info = svc.list_authorized().get(peer, {})
    new_name = xbmcgui.Dialog().input("Rename device",
                                       defaultt=info.get("name", ""))
    if new_name:
        svc.rename_device(peer, new_name)


def action_forget(router):
    peer = router.args.get("peer")
    if not peer:
        return
    if _HAS_KODI:
        import xbmcgui
        if not xbmcgui.Dialog().yesno("Forget device",
                                       "Forget this paired device?"):
            return
    svc = get_service()
    svc.remove_authorized(peer)
    notify("Device forgotten")


# -- sync data flow ---------------------------------------------------------

def _authorized_payload(svc, sess):
    id_str = sess.session_id.hex()
    id_bytes = id_str.encode("utf-8")
    name_bytes = svc.device_name.encode("utf-8")
    return bytes([len(id_bytes)]) + id_bytes + bytes([len(name_bytes)]) + name_bytes


def run_full_sync(sess):
    """Push our subscriptions + groups to the peer; pull theirs back.
    Newest-timestamp-wins for each record."""
    import base64
    from ..sources import subscriptions as subs, groups as grp
    import json as _json

    svc = get_service()
    peer_pub = sess.remote_public_key
    peer_pub_b64 = base64.b64encode(peer_pub).decode("ascii")
    local_pub_b64 = base64.b64encode(svc.local_pub).decode("ascii")

    # 1) Subscriptions
    local_subs = subs.list_subscriptions()
    sub_bytes = _json.dumps(local_subs).encode("utf-8")
    records.publish_to(sess, peer_pub_b64, "subscriptions", sub_bytes)
    log("pushed subscriptions (%d entries, %d bytes)" % (len(local_subs), len(sub_bytes)), "info")

    pulled = records.fetch_from(sess, svc.local_priv, peer_pub_b64, local_pub_b64,
                                 "subscriptions")
    if pulled is not None:
        ts, data = pulled
        try:
            theirs = _json.loads(data.decode("utf-8"))
        except Exception as exc:
            log("subscriptions pull: bad json: %s" % exc, "warning")
            theirs = []
        _merge_subscriptions(theirs)
        log("pulled subscriptions (%d entries)" % len(theirs), "info")

    # 2) Groups
    local_groups = grp.list_groups()
    grp_bytes = _json.dumps(local_groups).encode("utf-8")
    records.publish_to(sess, peer_pub_b64, "groups", grp_bytes)
    log("pushed groups (%d entries, %d bytes)" % (len(local_groups), len(grp_bytes)), "info")

    pulled_g = records.fetch_from(sess, svc.local_priv, peer_pub_b64, local_pub_b64,
                                   "groups")
    if pulled_g is not None:
        ts, data = pulled_g
        try:
            theirs = _json.loads(data.decode("utf-8"))
        except Exception as exc:
            log("groups pull: bad json: %s" % exc, "warning")
            theirs = []
        _merge_groups(theirs)
        log("pulled groups (%d entries)" % len(theirs), "info")


def _merge_subscriptions(theirs):
    """Merge the peer's subscriptions with ours. Union by (source,url)."""
    from ..sources import subscriptions as subs
    ours = subs.list_subscriptions()
    ours_keys = {(s.get("source"), s.get("url")) for s in ours}
    added = 0
    for s in theirs:
        k = (s.get("source"), s.get("url"))
        if k in ours_keys:
            continue
        if not s.get("source") or not s.get("url"):
            continue
        if subs.add_subscription(s["source"], s["url"],
                                  s.get("name", ""), s.get("thumbnail", "")):
            added += 1
    if added:
        log("merged %d subscriptions from peer" % added, "info")


def _merge_groups(theirs):
    """Merge the peer's groups with ours. Union by group id; members by (source,url)."""
    from ..sources import groups as grp
    ours = grp.list_groups()
    ours_by_id = {g.get("id"): g for g in ours}
    added = 0
    for g in theirs:
        gid = g.get("id")
        name = g.get("name", gid or "group")
        if not gid:
            continue
        if gid not in ours_by_id:
            ours_by_id[gid] = grp.create_group(name)
            added += 1
        target = ours_by_id[gid]
        existing_members = {(m.get("source"), m.get("url")) for m in target.get("members", [])}
        for m in g.get("members", []):
            k = (m.get("source"), m.get("url"))
            if not m.get("source") or not m.get("url"):
                continue
            if k in existing_members:
                continue
            grp.add_member(gid, m["source"], m["url"], m.get("name", ""))
            existing_members.add(k)
    if added:
        log("added %d groups from peer" % added, "info")


# -- Kodi guard --------------------------------------------------------------

try:
    import xbmc
    import xbmcgui
    _HAS_KODI = True
except ImportError:
    _HAS_KODI = False
    xbmc = xbmcgui = None