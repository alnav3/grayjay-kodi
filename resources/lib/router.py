# -*- coding: utf-8 -*-
"""URL routing + Kodi list rendering.

Plugin URLs look like:
    plugin://plugin.video.grayjay/?action=home&source=<id>&page=<token>
    plugin://plugin.video.grayjay/?action=play&source=<id>&url=<contentUrl>
"""
import time
from urllib.parse import parse_qsl, urlencode

from .kodiutils import log, notify, ADDON_ID

try:
    import xbmc
    import xbmcgui
    import xbmcplugin
    _HAS_KODI = True
except ImportError:
    _HAS_KODI = False
    xbmc = xbmcgui = xbmcplugin = None


# -- display formatting (Grayjay-style metadata) --------------------------
def _human_count(n):
    """Compact view count like Grayjay: 532, 12K, 1.2M, 3.4B."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    for div, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= div:
            val = n / float(div)
            # 12.0K -> 12K, 1.2M stays 1.2M
            return ("%.1f" % val).rstrip("0").rstrip(".") + suffix
    return str(n)


def _relative_time(unix_seconds):
    """'3 days ago' style age from a unix timestamp, Grayjay-style."""
    try:
        delta = int(time.time()) - int(unix_seconds)
    except (TypeError, ValueError):
        return ""
    if delta < 0:
        return "scheduled"
    if delta < 60:
        return "just now"
    for secs, unit in ((31_536_000, "year"), (2_592_000, "month"),
                       (604_800, "week"), (86_400, "day"),
                       (3_600, "hour"), (60, "minute")):
        if delta >= secs:
            n = delta // secs
            return "%d %s%s ago" % (n, unit, "" if n == 1 else "s")
    return "just now"


def _fmt_date(unix_seconds):
    """YYYY-MM-DD (local) for Kodi's premiered/aired infolabel."""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(unix_seconds)))
    except (TypeError, ValueError, OSError):
        return ""


def _fmt_datetime(unix_seconds):
    """YYYY-MM-DD HH:MM:SS (local) for Kodi's dateadded infolabel."""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(unix_seconds)))
    except (TypeError, ValueError, OSError):
        return ""


class Router(object):
    def __init__(self, argv):
        self.base_url = argv[0]
        self.handle = int(argv[1]) if len(argv) > 1 else -1
        self.args = dict(parse_qsl(argv[2][1:])) if len(argv) > 2 else {}

    def url_for(self, **kwargs):
        return "%s?%s" % (self.base_url, urlencode(kwargs))

    def dispatch(self):
        action = self.args.get("action", "root")
        handler = getattr(self, "action_%s" % action, None)
        if handler is None:
            log("unknown action: %s" % action, "warning")
            handler = self.action_root
        try:
            handler()
        except Exception as exc:
            log("dispatch error in %s: %s" % (action, exc), "error")
            notify("Error: %s" % exc)
            if _HAS_KODI and self.handle >= 0:
                xbmcplugin.endOfDirectory(self.handle, succeeded=False)

    # -- bridge reuse -----------------------------------------------------
    def _bridge(self, source_id):
        """Return an enabled PluginBridge for a source, cached per request so
        aggregating many channels from one source only parses its JS once."""
        cache = getattr(self, "_bridges", None)
        if cache is None:
            cache = self._bridges = {}
        if source_id in cache:
            return cache[source_id]
        from .sources import manager, plugin_settings
        from .engine.bridge import PluginBridge
        cfg = manager.get_source(source_id)
        if cfg is None:
            cache[source_id] = None
            return None
        bridge = PluginBridge(cfg)
        bridge.enable(settings=plugin_settings.load(cfg))
        cache[source_id] = bridge
        return bridge

    def _run(self, source_id, method, args):
        """Call a source method, returning a flat results list ([] on error)."""
        bridge = self._bridge(source_id)
        if bridge is None:
            return []
        try:
            result = bridge.call(method, args)
        except Exception as exc:
            log("%s.%s failed: %s" % (source_id, method, exc), "warning")
            return []
        if isinstance(result, dict):
            return result.get("results", [])
        return result or []

    # -- top level --------------------------------------------------------
    def action_root(self):
        """List Subscriptions, installed sources, and management entries."""
        from .sources import manager
        items = []
        # Subscriptions first — the cross-platform feed of channels you follow.
        items.append((self.url_for(action="subscriptions"),
                      "[ Subscriptions ]", True, ""))
        for cfg in manager.list_sources():
            ctx = [
                ("Settings",
                 "RunPlugin(%s)" % self.url_for(action="source_settings", source=cfg.id)),
                ("Update %s" % cfg.name,
                 "RunPlugin(%s)" % self.url_for(action="update_source", source=cfg.id)),
            ]
            items.append((
                self.url_for(action="home", source=cfg.id),
                cfg.name, True, cfg.icon_url, None, ctx,
            ))
        items.append((self.url_for(action="add_source"), "[ Add source… ]", True, ""))
        items.append((self.url_for(action="update_sources"), "[ Check for updates… ]", False, ""))
        self._render(items)

    def action_add_source(self):
        """Submenu: enter a config URL by hand, or pick an official source."""
        from .sources import official, manager
        installed = {c.id for c in manager.list_sources()}
        installed_names = {c.name for c in manager.list_sources()}
        items = [(self.url_for(action="add_source_url"), "[ Enter source URL… ]", False, "")]
        for name, url in official.OFFICIAL_SOURCES:
            label = name
            # Best-effort "installed" hint (we don't fetch each config here).
            if name in installed_names or name.split(" (")[0] in installed:
                label = "%s  ✓" % name
            items.append((
                self.url_for(action="install_official", url=url, name=name),
                label, False, "",
            ))
        self._render(items)

    def action_add_source_url(self):
        if not _HAS_KODI:
            return
        url = xbmcgui.Dialog().input("Source config URL")
        if not url:
            return
        from .sources import manager
        try:
            manager.install_from_url(url)
        except Exception as exc:
            notify("Install failed: %s" % exc)
        # Return to the root list after adding.
        if _HAS_KODI:
            xbmc.executebuiltin("Container.Update(%s,replace)" % self.url_for(action="root"))

    def action_install_official(self):
        """Install one of Grayjay's official sources by its config URL."""
        url = self.args.get("url")
        name = self.args.get("name", "source")
        if not url:
            return
        if _HAS_KODI:
            notify("Installing %s…" % name)
        from .sources import manager
        try:
            manager.install_from_url(url)
        except Exception as exc:
            notify("Install failed: %s" % exc)
        if _HAS_KODI:
            xbmc.executebuiltin("Container.Update(%s,replace)" % self.url_for(action="root"))

    def action_update_sources(self):
        """Manually check all sources and apply any available updates."""
        from .sources import updates
        if _HAS_KODI:
            notify("Checking for source updates…")
        applied, checked = updates.update_all(notify_summary=False)
        errors = [c for c in checked if c.get("error")]
        if applied:
            notify("Updated %d source(s)" % len(applied))
        elif errors:
            notify("Update check: %d error(s)" % len(errors))
        else:
            notify("All %d source(s) up to date" % len(checked))
        if _HAS_KODI:
            xbmc.executebuiltin("Container.Refresh")

    def action_update_source(self):
        """Update a single source by id (from its context menu)."""
        from .sources import manager, updates
        source_id = self.args.get("source")
        cfg = manager.get_source(source_id)
        if cfg is None:
            notify("Source not found")
            return
        applied, info = updates.update_source(cfg)
        if applied:
            notify("Updated %s to v%s" % (cfg.name, info.get("applied_version")))
        elif info.get("error"):
            notify("Update failed: %s" % info["error"])
        else:
            notify("%s is up to date" % cfg.name)
        if _HAS_KODI:
            xbmc.executebuiltin("Container.Refresh")

    # -- per-source plugin settings ---------------------------------------
    def action_source_settings(self):
        """Edit a source's own plugin settings (e.g. YouTube's 'Allow Age
        Restricted'). These are declared by the plugin in its config and read in
        source.enable(); we persist overrides per source and pass them in on
        every call."""
        if not _HAS_KODI:
            return
        from .sources import manager, plugin_settings
        source_id = self.args.get("source")
        cfg = manager.get_source(source_id)
        if cfg is None:
            notify("Source not found")
            return
        descs = [d for d in plugin_settings.descriptors(cfg) if d.get("variable")
                 and (d.get("type") or "").lower() in ("boolean", "dropdown")]
        if not descs:
            notify("%s has no editable settings" % cfg.name)
            return
        values = plugin_settings.load(cfg)
        dialog = xbmcgui.Dialog()
        while True:
            labels = [self._setting_label(d, values) for d in descs]
            labels.append("[ Save & close ]")
            idx = dialog.select("%s settings" % cfg.name, labels)
            if idx < 0 or idx == len(descs):       # cancel or "Save & close"
                break
            d = descs[idx]
            var = d["variable"]
            t = (d.get("type") or "").lower()
            if t == "boolean":
                values[var] = not bool(values.get(var))
            elif t == "dropdown":
                opts = d.get("options") or []
                cur = int(values.get(var) or 0)
                pick = dialog.select(d.get("name", var), opts, preselect=cur)
                if pick >= 0:
                    values[var] = pick
        plugin_settings.save(cfg, values)
        notify("Saved %s settings" % cfg.name)
        # When opened as a clicked list item (has a directory handle), close the
        # empty directory so Kodi stays put instead of waiting for a listing.
        # When opened via the context menu (RunPlugin), handle is -1 — skip.
        if _HAS_KODI and self.handle >= 0:
            xbmcplugin.endOfDirectory(self.handle, succeeded=False)

    @staticmethod
    def _setting_label(desc, values):
        var = desc["variable"]
        name = desc.get("name", var)
        t = (desc.get("type") or "").lower()
        val = values.get(var)
        if t == "boolean":
            return "%s: %s" % (name, "ON" if val else "off")
        if t == "dropdown":
            opts = desc.get("options") or []
            try:
                shown = opts[int(val or 0)]
            except (IndexError, ValueError, TypeError):
                shown = str(val)
            return "%s: %s" % (name, shown)
        return "%s: %s" % (name, val)

    # -- per-source feeds -------------------------------------------------
    def action_home(self):
        """A source's landing page: actions (Search) followed by its home feed,
        the same shape as opening a source in the Grayjay app."""
        from .sources import manager
        source_id = self.args.get("source")
        cfg = manager.get_source(source_id)
        name = cfg.name if cfg else source_id
        items = [
            (self.url_for(action="search", source=source_id),
             "[ Search %s ]" % name, True, ""),
            (self.url_for(action="source_settings", source=source_id),
             "[ %s Settings ]" % name, False, ""),
        ]
        page = self.args.get("page") or None
        results = self._run(source_id, "getHome", [page])
        items += [self._content_item(source_id, v) for v in results]
        self._render(items, content_type="videos")

    def action_search(self):
        if not _HAS_KODI:
            return
        query = xbmcgui.Dialog().input("Search")
        if not query:
            return
        self._feed("search", extra_args=[query, None, None, []])

    def _feed(self, method, extra_args=None):
        source_id = self.args.get("source")
        page = self.args.get("page") or None
        results = self._run(source_id, method, (extra_args or []) + [page])
        items = [self._content_item(source_id, v) for v in results]
        self._render(items, content_type="videos")

    # -- subscriptions (cross-platform) -----------------------------------
    def action_subscriptions(self):
        """Subscriptions landing: 'All' plus one feed per group, like Grayjay's
        group tabs, then a management entry."""
        from .sources import subscriptions as subs, groups as grp
        all_subs = subs.list_subscriptions()
        if not all_subs:
            self._render([(self.url_for(action="root"),
                           "No subscriptions yet — use 'Subscribe' on any video", False, "")])
            return
        items = [(self.url_for(action="sub_feed"),
                  "[ All ]  (%d)" % len(all_subs), True, "")]
        for g in grp.list_groups():
            n = len(g.get("members", []))
            ctx = [
                ("Manage group",
                 "Container.Update(%s)" % self.url_for(action="group_manage", group=g["id"])),
                ("Rename group",
                 "RunPlugin(%s)" % self.url_for(action="group_rename", group=g["id"])),
                ("Delete group",
                 "RunPlugin(%s)" % self.url_for(action="group_delete", group=g["id"])),
            ]
            items.append((self.url_for(action="sub_feed", group=g["id"]),
                          "%s  (%d)" % (g.get("name", g["id"]), n), True, "", None, ctx))
        items.append((self.url_for(action="groups"), "[ Manage groups… ]", True, ""))
        self._render(items)

    def action_sub_feed(self):
        """Aggregate recent content across subscribed channels, newest first.
        With ?group=<id>, restrict to that group's members."""
        from .sources import subscriptions as subs, groups as grp
        group_id = self.args.get("group")
        feed_subs = subs.list_subscriptions()
        if group_id:
            g = grp.get_group(group_id)
            members = {(m.get("source"), m.get("url")) for m in (g or {}).get("members", [])}
            feed_subs = [s for s in feed_subs
                         if (s.get("source"), s.get("url")) in members]
            if not feed_subs:
                self._render([(self.url_for(action="subscriptions"),
                               "This group has no channels yet — add some from 'Manage groups'",
                               False, "")])
                return
        collected = []
        for s in feed_subs:
            results = self._run(s["source"], "getChannelContents",
                                [s["url"], None, None, [], None])
            for v in results:
                collected.append((s["source"], v))
        # Newest first when items carry a datetime (unix seconds).
        collected.sort(key=lambda sv: (sv[1].get("datetime") or 0), reverse=True)
        items = [self._content_item(src, v, show_source=True) for src, v in collected]
        self._render(items, content_type="videos")

    # -- subscription groups ----------------------------------------------
    def action_groups(self):
        """Management screen: list groups, create new ones."""
        from .sources import groups as grp
        items = [(self.url_for(action="group_create"), "[ Create group… ]", False, "")]
        for g in grp.list_groups():
            ctx = [
                ("Rename group",
                 "RunPlugin(%s)" % self.url_for(action="group_rename", group=g["id"])),
                ("Delete group",
                 "RunPlugin(%s)" % self.url_for(action="group_delete", group=g["id"])),
            ]
            items.append((self.url_for(action="group_manage", group=g["id"]),
                          "%s  (%d)" % (g.get("name", g["id"]), len(g.get("members", []))),
                          True, "", None, ctx))
        self._render(items)

    def action_group_create(self):
        if not _HAS_KODI:
            return
        name = xbmcgui.Dialog().input("Group name")
        if not name:
            return
        from .sources import groups as grp
        g = grp.create_group(name)
        notify("Created group %s" % g["name"])
        # Offer to populate it straight away.
        xbmc.executebuiltin("Container.Update(%s)" % self.url_for(action="group_manage", group=g["id"]))

    def action_group_rename(self):
        if not _HAS_KODI:
            return
        from .sources import groups as grp
        group_id = self.args.get("group")
        g = grp.get_group(group_id)
        if not g:
            return
        name = xbmcgui.Dialog().input("Rename group", defaultt=g.get("name", ""))
        if name and grp.rename_group(group_id, name):
            notify("Renamed to %s" % name)
        xbmc.executebuiltin("Container.Refresh")

    def action_group_delete(self):
        from .sources import groups as grp
        group_id = self.args.get("group")
        g = grp.get_group(group_id)
        if not g:
            return
        if _HAS_KODI:
            if not xbmcgui.Dialog().yesno("Delete group", "Delete '%s'?" % g.get("name", group_id)):
                return
        grp.delete_group(group_id)
        notify("Deleted group")
        if _HAS_KODI:
            # Leave the (now-gone) group screen back to the groups list.
            xbmc.executebuiltin("Container.Update(%s,replace)" % self.url_for(action="groups"))

    def action_group_manage(self):
        """A group's detail: its channels, plus add/remove entries."""
        from .sources import groups as grp
        group_id = self.args.get("group")
        g = grp.get_group(group_id)
        if not g:
            self._render([(self.url_for(action="groups"), "Group not found", False, "")])
            return
        items = [
            (self.url_for(action="group_addmembers", group=group_id),
             "[ Add / remove channels… ]", False, ""),
            (self.url_for(action="group_rename", group=group_id),
             "[ Rename group ]", False, ""),
        ]
        for m in g.get("members", []):
            label = m.get("name") or m.get("url")
            ctx = [("Remove from group",
                    "RunPlugin(%s)" % self.url_for(action="group_removemember",
                                                   group=group_id,
                                                   source=m.get("source"), url=m.get("url")))]
            items.append((self.url_for(action="channel", source=m.get("source"),
                                       url=m.get("url"), name=m.get("name", "")),
                          label, True, "", None, ctx))
        self._render(items)

    def action_group_addmembers(self):
        """Multiselect every subscription; the picked set becomes the group's
        members (pre-checking current members)."""
        if not _HAS_KODI:
            return
        from .sources import subscriptions as subs, groups as grp
        group_id = self.args.get("group")
        g = grp.get_group(group_id)
        if not g:
            return
        all_subs = subs.list_subscriptions()
        if not all_subs:
            notify("No subscriptions to add")
            return
        labels = ["%s  ·  %s" % (s.get("name") or s.get("url"),
                                 self._source_name(s.get("source"))) for s in all_subs]
        current = {(m.get("source"), m.get("url")) for m in g.get("members", [])}
        preselect = [i for i, s in enumerate(all_subs)
                     if (s.get("source"), s.get("url")) in current]
        chosen = xbmcgui.Dialog().multiselect("Channels in '%s'" % g.get("name", group_id),
                                              labels, preselect=preselect)
        if chosen is None:  # cancelled
            return
        members = [{"source": all_subs[i].get("source"), "url": all_subs[i].get("url"),
                    "name": all_subs[i].get("name", "")} for i in chosen]
        grp.set_members(group_id, members)
        notify("%d channel(s) in %s" % (len(members), g.get("name", group_id)))
        xbmc.executebuiltin("Container.Update(%s,replace)" % self.url_for(action="group_manage", group=group_id))

    def action_group_removemember(self):
        from .sources import groups as grp
        group_id = self.args.get("group")
        if grp.remove_member(group_id, self.args.get("source"), self.args.get("url")):
            notify("Removed from group")
        if _HAS_KODI:
            xbmc.executebuiltin("Container.Refresh")

    def action_subscribe(self):
        from .sources import subscriptions as subs
        source_id = self.args.get("source")
        url = self.args.get("url")
        name = self.args.get("name", "")
        if not source_id or not url:
            return
        if subs.add_subscription(source_id, url, name):
            notify("Subscribed to %s" % (name or url))
        else:
            notify("Already subscribed")
        if _HAS_KODI:
            xbmc.executebuiltin("Container.Refresh")

    def action_unsubscribe(self):
        from .sources import subscriptions as subs
        source_id = self.args.get("source")
        url = self.args.get("url")
        if subs.remove_subscription(source_id, url):
            notify("Unsubscribed")
        if _HAS_KODI:
            xbmc.executebuiltin("Container.Refresh")

    def action_channel(self):
        """Browse a channel's contents, with a subscribe/unsubscribe entry."""
        from .sources import subscriptions as subs
        source_id = self.args.get("source")
        url = self.args.get("url")
        name = self.args.get("name", "")
        items = []
        if subs.is_subscribed(source_id, url):
            items.append((self.url_for(action="unsubscribe", source=source_id, url=url),
                          "[ Unsubscribe ]", False, ""))
            items.append((self.url_for(action="channel_groups", source=source_id, url=url, name=name),
                          "[ Add to groups… ]", False, ""))
        else:
            items.append((self.url_for(action="subscribe", source=source_id, url=url, name=name),
                          "[ Subscribe ]", False, ""))
        for v in self._run(source_id, "getChannelContents", [url, None, None, [], None]):
            items.append(self._content_item(source_id, v))
        self._render(items, content_type="videos")

    def action_channel_groups(self):
        """Toggle this channel's membership across all groups via multiselect."""
        if not _HAS_KODI:
            return
        from .sources import groups as grp
        source_id = self.args.get("source")
        url = self.args.get("url")
        name = self.args.get("name", "")
        all_groups = grp.list_groups()
        if not all_groups:
            if xbmcgui.Dialog().yesno("No groups", "No groups exist yet. Create one now?"):
                self.action_group_create()
            return
        labels = ["%s  (%d)" % (g.get("name", g["id"]), len(g.get("members", []))) for g in all_groups]
        preselect = [i for i, g in enumerate(all_groups) if grp.is_member(g["id"], source_id, url)]
        chosen = xbmcgui.Dialog().multiselect("Add '%s' to groups" % (name or url),
                                              labels, preselect=preselect)
        if chosen is None:
            return
        chosen = set(chosen)
        for i, g in enumerate(all_groups):
            if i in chosen:
                grp.add_member(g["id"], source_id, url, name)
            else:
                grp.remove_member(g["id"], source_id, url)
        notify("Updated groups for %s" % (name or url))
        xbmc.executebuiltin("Container.Refresh")

    def _source_name(self, source_id):
        """Display name for a source id, cached for the request."""
        cache = getattr(self, "_src_names", None)
        if cache is None:
            from .sources import manager
            cache = self._src_names = {c.id: c.name for c in manager.list_sources()}
        return cache.get(source_id, source_id)

    def _content_item(self, source_id, v, show_source=False):
        title = v.get("name", "Untitled")
        url = self.url_for(action="play", source=source_id, url=v.get("url", ""))
        thumb = ""
        thumbs = (v.get("thumbnails") or {}).get("sources") or []
        if thumbs:
            thumb = thumbs[-1].get("url", "")
        info = self._video_info(source_id, v, show_source)
        return (url, title, False, thumb, info, self._context_menu(source_id, v))

    def _video_info(self, source_id, v, show_source):
        """Grayjay-style metadata for a video item: the
        '<source> • <channel> • <views> views • <relative time>' line, plus the
        infolabels (date, duration, plot) that Kodi skins surface."""
        author = (v.get("author") or {}).get("name", "")
        views = v.get("viewCount") or 0
        dt = v.get("datetime") or 0
        dur = v.get("duration") or 0
        is_live = bool(v.get("isLive"))
        src = self._source_name(source_id)

        parts = []
        if show_source and src:
            parts.append(src)
        if author:
            parts.append(author)
        if is_live:
            parts.append("LIVE")
        elif views and views > 0:
            parts.append("%s views" % _human_count(views))
        if dt:
            parts.append(_relative_time(dt))
        metaline = "  •  ".join(parts)

        desc = v.get("description") or ""
        plot = "%s\n\n%s" % (metaline, desc) if (metaline and desc) else (desc or metaline)

        return {
            "mediatype": "video",
            "label2": metaline,
            "plot": plot,
            "duration": int(dur) if dur else 0,
            "studio": src,
            "author": author,
            "premiered": _fmt_date(dt) if dt else "",
            "dateadded": _fmt_datetime(dt) if dt else "",
            "live": is_live,
        }

    def _context_menu(self, source_id, v):
        """Right-click menu: Subscribe to / Unsubscribe from this video's
        channel (a local, cross-platform subscription via this addon)."""
        author = v.get("author") or {}
        ch_url = author.get("url")
        ch_name = author.get("name", "")
        if not ch_url:
            return []
        from .sources import subscriptions as subs
        menu = []
        if subs.is_subscribed(source_id, ch_url):
            menu.append(("Unsubscribe from %s" % ch_name,
                         "RunPlugin(%s)" % self.url_for(action="unsubscribe", source=source_id, url=ch_url)))
        else:
            menu.append(("Subscribe to %s" % ch_name,
                         "RunPlugin(%s)" % self.url_for(action="subscribe", source=source_id, url=ch_url, name=ch_name)))
        menu.append(("Go to channel",
                     "Container.Update(%s)" % self.url_for(action="channel", source=source_id, url=ch_url, name=ch_name)))
        return menu

    def action_play(self):
        from .sources import manager, plugin_settings
        from .engine.bridge import PluginBridge

        source_id = self.args.get("source")
        content_url = self.args.get("url")
        cfg = manager.get_source(source_id)
        if cfg is None:
            notify("Source not found")
            return
        bridge = PluginBridge(cfg)
        bridge.enable(settings=plugin_settings.load(cfg))
        details = bridge.call("getContentDetails", [content_url])

        # Preferred: a ready-made manifest/progressive URL (PeerTube HLS, etc.).
        play_url = self._pick_stream(details)
        if play_url:
            if _HAS_KODI:
                li = xbmcgui.ListItem(path=play_url)
                xbmcplugin.setResolvedUrl(self.handle, True, li)
            else:
                log("would play: %s" % play_url, "info")
            return

        # Fallback: adaptive video-only + audio-only tracks (YouTube). Build a
        # DASH manifest from the direct-URL formats harvested off the player
        # response and let inputstream.adaptive mux them.
        mpd_path = self._build_dash(cfg, bridge, details)
        if mpd_path:
            if _HAS_KODI:
                li = xbmcgui.ListItem(path=mpd_path)
                li.setMimeType("application/dash+xml")
                li.setContentLookup(False)
                li.setProperty("inputstream", "inputstream.adaptive")
                li.setProperty("inputstream.adaptive.manifest_type", "mpd")
                xbmcplugin.setResolvedUrl(self.handle, True, li)
            else:
                log("would play DASH manifest: %s" % mpd_path, "info")
            return

        notify("No playable stream found")

    def _build_dash(self, cfg, bridge, details):
        """Synthesise a DASH MPD from harvested adaptive formats; return a path
        to the written manifest, or None."""
        from .playback import mpd as mpd_builder
        from .kodiutils import profile_path
        import os

        formats = bridge.harvested_streams()
        if not formats:
            return None
        dur_ms = 0
        try:
            dur_ms = int((details or {}).get("duration") or 0) * 1000
        except (TypeError, ValueError):
            dur_ms = 0
        manifest = mpd_builder.build_mpd(formats, dur_ms or None)
        if not manifest:
            return None
        cache = os.path.join(profile_path(), "cache")
        if not os.path.isdir(cache):
            os.makedirs(cache)
        path = os.path.join(cache, "stream_%s.mpd" % cfg.id)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(manifest)
        return path

    def _pick_stream(self, details):
        """Pick a directly-playable URL: a real HLS/DASH *manifest* or a muxed
        progressive stream. Returns None for adaptive raw/ABR tracks (e.g.
        YouTube's video-only DashRawSource) — those are handled by _build_dash,
        which combines them with the audio track into a manifest."""
        if not details:
            return None
        desc = details.get("video") or {}
        sources = desc.get("videoSources") or []

        # source.js tags the class via `plugin_type` (Grayjay), older shims via
        # `type`.
        def kind(s):
            return s.get("plugin_type") or s.get("type") or ""

        # Raw adaptive tracks are not independently playable (video-only / SABR).
        def is_raw(k):
            return "Raw" in k or "ABR" in k

        # A real manifest source (HLSSource / DashManifestSource) points at an
        # .m3u8/.mpd we can hand straight to Kodi.
        for s in sources:
            k = kind(s)
            if is_raw(k):
                continue
            if "HLS" in k or "Manifest" in k:
                return s.get("url")
        # Otherwise the best muxed progressive stream (highest resolution).
        best, best_h = None, -1
        for s in sources:
            if is_raw(kind(s)):
                continue
            h = s.get("height") or 0
            if h >= best_h and s.get("url"):
                best, best_h = s.get("url"), h
        return best

    # -- rendering --------------------------------------------------------
    def _render(self, items, content_type=None):
        if not _HAS_KODI or self.handle < 0:
            for it in items:
                log("ITEM %s -> %s" % (it[1], it[0]), "info")
            return
        if content_type:
            xbmcplugin.setContent(self.handle, content_type)
        for it in items:
            url, label, is_folder = it[0], it[1], it[2]
            thumb = it[3] if len(it) > 3 else ""
            info = it[4] if len(it) > 4 else None
            context = it[5] if len(it) > 5 else None
            li = xbmcgui.ListItem(label=label)
            if thumb:
                li.setArt({"thumb": thumb, "icon": thumb, "poster": thumb})
            if isinstance(info, dict):
                self._apply_video_info(li, label, info)
            if not is_folder:
                li.setProperty("IsPlayable", "true")
            if context:
                li.addContextMenuItems(context)
            xbmcplugin.addDirectoryItem(self.handle, url, li, is_folder)
        xbmcplugin.endOfDirectory(self.handle)

    @staticmethod
    def _apply_video_info(li, label, info):
        """Map our metadata dict onto a Kodi ListItem. Uses InfoTagVideo (Kodi
        20+/Omega) and falls back to setInfo on older builds. The Grayjay-style
        '<source> • <channel> • <views> • <time>' line goes to label2 so skins
        that show a second line render it; everything also lands in infolabels
        (date/duration/plot) for skins and the info dialog."""
        meta = info.get("label2")
        if meta:
            li.setLabel2(meta)
        try:
            vt = li.getVideoInfoTag()
            vt.setMediaType("video")
            vt.setTitle(label)
            if info.get("plot"):
                vt.setPlot(info["plot"])
            if info.get("duration"):
                vt.setDuration(info["duration"])
            if info.get("studio"):
                vt.setStudios([info["studio"]])
            if info.get("author"):
                vt.setDirectors([info["author"]])
            if info.get("premiered"):
                vt.setPremiered(info["premiered"])
            if info.get("dateadded"):
                vt.setDateAdded(info["dateadded"])
        except Exception:
            # Older Kodi without the InfoTagVideo setters.
            data = {"mediatype": "video", "title": label}
            if info.get("plot"):
                data["plot"] = info["plot"]
            if info.get("duration"):
                data["duration"] = info["duration"]
            if info.get("studio"):
                data["studio"] = info["studio"]
            if info.get("author"):
                data["director"] = info["author"]
            if info.get("premiered"):
                data["premiered"] = info["premiered"]
            if info.get("dateadded"):
                data["dateadded"] = info["dateadded"]
            try:
                li.setInfo("video", data)
            except Exception:
                pass
