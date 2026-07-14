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
        from .sources import manager, plugin_settings, plugin_state
        from .engine.bridge import PluginBridge
        cfg = manager.get_source(source_id)
        if cfg is None:
            cache[source_id] = None
            return None
        bridge = PluginBridge(cfg)
        # Feed back the plugin's persisted saveState() so a fresh Kodi plugin
        # process doesn't redo expensive session init (YouTube: innertube
        # context + BotGuard) on every single invocation.
        bridge.enable(settings=plugin_settings.load(cfg),
                      saved_state=plugin_state.load(cfg) or None)
        self._persist_state(bridge)
        cache[source_id] = bridge
        return bridge

    @staticmethod
    def _persist_state(bridge):
        """Best-effort: store the source's current saveState() for next launch."""
        try:
            from .sources import plugin_state
            state = bridge.save_state()
            if state:
                plugin_state.save(bridge.config, state)
        except Exception as exc:
            log("persisting state failed: %s" % exc, "debug")

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
        items.append((self.url_for(action="on_deck"), "[ On Deck ]", True, ""))
        items.append((self.url_for(action="history"), "[ History ]", True, ""))
        for cfg in manager.list_sources():
            ctx = [
                ("Settings",
                 "RunPlugin(%s)" % self.url_for(action="source_settings", source=cfg.id)),
                ("Update %s" % cfg.name,
                 "RunPlugin(%s)" % self.url_for(action="update_source", source=cfg.id)),
                ("Remove %s" % cfg.name,
                 "RunPlugin(%s)" % self.url_for(action="remove_source", source=cfg.id)),
            ]
            items.append((
                self.url_for(action="home", source=cfg.id),
                cfg.name, True, cfg.icon_url, None, ctx,
            ))
        items.append((self.url_for(action="add_source"), "[ Add source… ]", True, ""))
        items.append((self.url_for(action="update_sources"), "[ Check for updates… ]", False, ""))
        # Sync submenu — only shown when the addon setting is on, so users
        # who haven't enabled sync don't see an inert entry.
        from .kodiutils import get_setting
        if get_setting("sync_enabled", "false") == "true":
            items.append((self.url_for(action="sync_root"), "[ Sync ]", True, ""))
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
        applied, checked = updates.update_all()
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

    def action_remove_source(self):
        """Uninstall a source (with confirmation) from its context menu."""
        from .sources import manager
        source_id = self.args.get("source")
        cfg = manager.get_source(source_id)
        if cfg is None:
            notify("Source not found")
            return
        if _HAS_KODI:
            if not xbmcgui.Dialog().yesno("Remove source",
                                          "Remove '%s'? Its subscriptions are kept." % cfg.name):
                return
        if manager.remove_source(source_id):
            notify("Removed %s" % cfg.name)
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

    # -- watch history / on deck --------------------------------------------
    def action_on_deck(self):
        """Partially watched videos, most recent first, with resume points."""
        from . import watch
        entries = watch.list_on_deck()
        if not entries:
            self._render([(self.url_for(action="root"),
                           "Nothing on deck — partially watched videos land here",
                           False, "")])
            return
        items = [self._watch_item(e, [
            ("Mark as watched",
             "RunPlugin(%s)" % self.url_for(action="watch_markwatched",
                                            source=e.get("source"), url=e.get("url"))),
            ("Remove from On Deck",
             "RunPlugin(%s)" % self.url_for(action="watch_remove",
                                            source=e.get("source"), url=e.get("url"))),
        ]) for e in entries]
        self._render(items, content_type="videos")

    def action_history(self):
        """Watched videos, most recently watched first."""
        from . import watch
        entries = watch.list_history()
        if not entries:
            self._render([(self.url_for(action="root"),
                           "No watch history yet", False, "")])
            return
        items = [self._watch_item(e, [
            ("Remove from history",
             "RunPlugin(%s)" % self.url_for(action="watch_remove",
                                            source=e.get("source"), url=e.get("url"))),
            ("Clear all history",
             "RunPlugin(%s)" % self.url_for(action="history_clear")),
        ]) for e in entries]
        self._render(items, content_type="videos")

    def _watch_item(self, e, extra_ctx):
        """A watch-state entry rendered like a feed item; thumbnail, duration
        and the 'x ago' line (last watched) come from the stored entry rather
        than a source call."""
        v = {
            "name": e.get("name") or e.get("url"),
            "url": e.get("url"),
            "thumbnails": {"sources": [{"url": e.get("thumbnail", "")}]},
            "duration": e.get("duration") or 0,
            "datetime": e.get("updated") or 0,
        }
        return self._content_item(e.get("source"), v, show_source=True,
                                  extra_ctx=extra_ctx)

    def action_watch_remove(self):
        from . import watch
        if watch.remove(self.args.get("source"), self.args.get("url")):
            notify("Removed")
        if _HAS_KODI:
            xbmc.executebuiltin("Container.Refresh")

    def action_watch_markwatched(self):
        from . import watch
        watch.mark_watched(self.args.get("source"), self.args.get("url"))
        notify("Marked as watched")
        if _HAS_KODI:
            xbmc.executebuiltin("Container.Refresh")

    def action_history_clear(self):
        from . import watch
        if _HAS_KODI:
            if not xbmcgui.Dialog().yesno("Clear history",
                                          "Remove all watched items from history?"):
                return
        watch.clear_history()
        notify("History cleared")
        if _HAS_KODI:
            xbmc.executebuiltin("Container.Refresh")

    def _source_name(self, source_id):
        """Display name for a source id, cached for the request."""
        cache = getattr(self, "_src_names", None)
        if cache is None:
            from .sources import manager
            cache = self._src_names = {c.id: c.name for c in manager.list_sources()}
        return cache.get(source_id, source_id)

    def _content_item(self, source_id, v, show_source=False, extra_ctx=None):
        title = v.get("name", "Untitled")
        url = self.url_for(action="play", source=source_id, url=v.get("url", ""))
        thumb = ""
        thumbs = (v.get("thumbnails") or {}).get("sources") or []
        if thumbs:
            thumb = thumbs[-1].get("url", "")
        info = self._video_info(source_id, v, show_source)
        ctx = (extra_ctx or []) + self._context_menu(source_id, v)
        return (url, title, False, thumb, info, ctx)

    def _watch_entry(self, source_id, url):
        """This video's watch-state entry (resume point / play count), from a
        per-request snapshot of the whole state file."""
        cache = getattr(self, "_watch_map", None)
        if cache is None:
            from . import watch
            cache = self._watch_map = {
                (e.get("source"), e.get("url")): e for e in watch.all_entries()}
        return cache.get((source_id, url))

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

        entry = self._watch_entry(source_id, v.get("url")) or {}
        playcount = int(entry.get("playcount") or 0)
        resume = float(entry.get("position") or 0)
        resume_total = float(entry.get("duration") or 0) or float(dur or 0)

        parts = []
        if show_source and src:
            parts.append(src)
        if author:
            parts.append(author)
        if is_live:
            parts.append("LIVE")
        elif views and views > 0:
            parts.append("%s views" % _human_count(views))
        if resume > 0 and resume_total > resume:
            parts.append("%d%% watched" % round(100.0 * resume / resume_total))
        if dt:
            parts.append(_relative_time(dt))
        metaline = "  •  ".join(parts)

        desc = v.get("description") or ""
        plot = "%s\n\n%s" % (metaline, desc) if (metaline and desc) else (desc or metaline)

        info = {
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
        if playcount:
            info["playcount"] = playcount
        if resume > 0 and resume_total > resume:
            info["resume"] = resume
            info["total"] = resume_total
        return info

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
        source_id = self.args.get("source")
        content_url = self.args.get("url")
        t0 = time.time()
        bridge = self._bridge(source_id)
        if bridge is None:
            notify("Source not found")
            return
        cfg = bridge.config
        t_enable = time.time() - t0
        details = bridge.call("getContentDetails", [content_url])
        log("play: enable %.1fs, getContentDetails %.1fs (%s)"
            % (t_enable, time.time() - t0 - t_enable, source_id), "info")
        # The details call refreshes session tokens; keep them for next launch.
        self._persist_state(bridge)

        # Preferred: a ready-made manifest/progressive URL (PeerTube HLS, etc.).
        play_url = self._pick_stream(details)
        if play_url:
            self._handoff_now_playing(source_id, content_url, details)
            if _HAS_KODI:
                li = xbmcgui.ListItem(path=play_url)
                xbmcplugin.setResolvedUrl(self.handle, True, li)
            else:
                log("would play: %s" % play_url, "info")
            return

        # Preferred fallback: full-quality adaptive (video-only + audio-only)
        # tracks combined into a DASH manifest for inputstream.adaptive. ISA
        # only loads a manifest over HTTP, so this needs the background service's
        # manifest server to be up.
        manifest_url = self._build_dash(cfg, bridge, details)
        if manifest_url:
            self._handoff_now_playing(source_id, content_url, details)
            if _HAS_KODI:
                li = xbmcgui.ListItem(path=manifest_url)
                li.setMimeType("application/dash+xml")
                li.setContentLookup(False)
                li.setProperty("inputstream", "inputstream.adaptive")
                xbmcplugin.setResolvedUrl(self.handle, True, li)
            else:
                log("would play DASH manifest: %s" % manifest_url, "info")
            return

        # Fallback: a muxed (progressive) stream — a single URL Kodi's native
        # player handles directly, no ISA needed. Reliable but capped at the
        # qualities YouTube still muxes (usually 360p, itag 18). Used when the
        # manifest server isn't available or there are no adaptive tracks.
        muxed_url = self._pick_muxed(bridge)
        if muxed_url:
            self._handoff_now_playing(source_id, content_url, details)
            if _HAS_KODI:
                li = xbmcgui.ListItem(path=muxed_url)
                li.setContentLookup(False)
                xbmcplugin.setResolvedUrl(self.handle, True, li)
            else:
                log("would play muxed: %s" % muxed_url, "info")
            return

        notify("No playable stream found")

    def _handoff_now_playing(self, source_id, content_url, details):
        """Leave the background service a note about what is about to play so
        it can track watch progress and queue "Up next" — this process exits
        right after setResolvedUrl and can't observe the player itself."""
        try:
            from . import watch
            qid = self.args.get("qid", "")
            idx = self.args.get("idx", "")
            d = details or {}
            name = d.get("name") or ""
            duration = d.get("duration") or 0
            thumb = ""
            thumbs = (d.get("thumbnails") or {}).get("sources") or []
            if thumbs:
                thumb = thumbs[-1].get("url", "")
            if qid and (not name or not thumb or not duration):
                qi = watch.queue_item(qid, idx)
                if qi:
                    name = name or qi.get("name", "")
                    thumb = thumb or qi.get("thumbnail", "")
                    duration = duration or qi.get("duration") or 0
            watch.set_now_playing({
                "source": source_id, "url": content_url, "name": name,
                "thumbnail": thumb, "duration": duration,
                "qid": qid, "idx": idx,
            })
        except Exception as exc:
            log("now-playing handoff failed: %s" % exc, "debug")

    def _pick_muxed(self, bridge):
        """Highest-resolution muxed/progressive URL harvested from the player
        response (single audio+video stream), or None."""
        muxed = bridge.harvested_muxed() if hasattr(bridge, "harvested_muxed") else []
        best, best_h = None, -1
        for f in muxed or []:
            if not f.get("url"):
                continue
            h = f.get("height") or 0
            if h >= best_h:
                best, best_h = f.get("url"), h
        return best

    def _build_dash(self, cfg, bridge, details):
        """Synthesise a DASH MPD from harvested adaptive formats and stage it
        on the loopback manifest server; return the http:// URL ISA should
        fetch, or None (caller falls back to a muxed stream).

        Media URLs in the manifest point back at the same loopback server,
        which relays them to googlevideo translating ISA's HTTP Range requests
        into the `range=` query parameter the CDN reliably honors — otherwise
        seeks are applied to one track but not the other (or fail outright)."""
        import os
        import socket
        from .playback import mpd as mpd_builder, manifest_server
        from .kodiutils import profile_path, get_setting

        formats = bridge.harvested_streams()
        if not formats:
            return None
        # ISA only loads manifests over HTTP, so without the background
        # service's server there is no DASH playback at all.
        profile = profile_path()
        port = manifest_server.published_port(profile)
        if not port:
            return None
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
        except OSError:
            return None

        try:
            max_height = int(get_setting("max_video_height", "1080"))
        except (TypeError, ValueError):
            max_height = 1080
        adaptive = get_setting("adaptive_quality", "false") == "true"
        secret = manifest_server.proxy_secret(profile)

        def proxied(fmt):
            try:
                content_length = int(fmt.get("contentLength") or 0)
            except (TypeError, ValueError):
                content_length = 0
            mime = (fmt.get("mimeType") or "").split(";")[0].strip()
            return manifest_server.media_url(port, secret, fmt.get("url"),
                                             content_length=content_length,
                                             mime=mime)

        dur_ms = 0
        try:
            dur_ms = int((details or {}).get("duration") or 0) * 1000
        except (TypeError, ValueError):
            dur_ms = 0
        manifest = mpd_builder.build_mpd(formats, dur_ms or None,
                                         url_map=proxied,
                                         max_height=max_height,
                                         adaptive=adaptive)
        if not manifest:
            return None
        cache = os.path.join(profile, "cache")
        if not os.path.isdir(cache):
            os.makedirs(cache)
        path = os.path.join(cache, "stream_%s.mpd" % cfg.id)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(manifest)
        return "http://127.0.0.1:%d/%s" % (port, os.path.basename(path))

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

    # -- sync -------------------------------------------------------------
    def action_sync_root(self):
        from .sync import router_actions as sa
        self._render(sa.action_root(self))

    def action_sync_show_pairing_url(self):
        from .sync import router_actions as sa
        sa.action_show_pairing_url(self)

    def action_sync_pair_url(self):
        from .sync import router_actions as sa
        sa.action_pair_url(self)

    def action_sync_devices(self):
        from .sync import router_actions as sa
        self._render(sa.action_devices(self))

    def action_sync_now_one(self):
        from .sync import router_actions as sa
        sa.action_now_one(self)

    def action_sync_now_all(self):
        from .sync import router_actions as sa
        sa.action_now_all(self)

    def action_sync_rename(self):
        from .sync import router_actions as sa
        sa.action_rename(self)

    def action_sync_forget(self):
        from .sync import router_actions as sa
        sa.action_forget(self)

    # -- rendering --------------------------------------------------------
    def _attach_queue(self, items):
        """Snapshot this listing's playable items and tag their play URLs with
        a queue id + index, so the player monitor can offer "Up next" — the
        following item from whatever list playback was started from."""
        playable = []
        for i, it in enumerate(items):
            query = it[0].split("?", 1)
            q = dict(parse_qsl(query[1])) if len(query) == 2 else {}
            if q.get("action") == "play" and q.get("url"):
                playable.append((i, q))
        if not playable:
            return items
        from . import watch
        import json
        # Keyed on listing identity + contents: re-rendering the same list
        # reuses one snapshot file, a changed list gets a fresh one so stale
        # play URLs still resolve against the queue they were rendered with.
        qid = watch.make_queue_id(json.dumps(
            {"args": self.args, "urls": [q["url"] for _, q in playable]},
            sort_keys=True))
        out = list(items)
        queue = []
        for pos, (i, q) in enumerate(playable):
            it = list(out[i])
            it[0] = "%s&%s" % (it[0], urlencode({"qid": qid, "idx": pos}))
            out[i] = tuple(it)
            info = it[4] if len(it) > 4 and isinstance(it[4], dict) else {}
            queue.append({"play": it[0], "source": q.get("source"),
                          "url": q["url"], "name": it[1],
                          "thumbnail": it[3] if len(it) > 3 else "",
                          "duration": info.get("duration") or 0})
        watch.save_queue(qid, queue)
        return out

    def _render(self, items, content_type=None):
        items = self._attach_queue(items)
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
            if info.get("playcount"):
                try:
                    vt.setPlaycount(int(info["playcount"]))
                except Exception:
                    pass
            if info.get("resume") and info.get("total"):
                # Resume point → Kodi offers "Resume from …" and seeks there.
                try:
                    vt.setResumePoint(float(info["resume"]), float(info["total"]))
                except Exception:
                    li.setProperty("ResumeTime", str(info["resume"]))
                    li.setProperty("TotalTime", str(info["total"]))
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
            if info.get("playcount"):
                data["playcount"] = int(info["playcount"])
            try:
                li.setInfo("video", data)
            except Exception:
                pass
            if info.get("resume") and info.get("total"):
                li.setProperty("ResumeTime", str(info["resume"]))
                li.setProperty("TotalTime", str(info["total"]))
