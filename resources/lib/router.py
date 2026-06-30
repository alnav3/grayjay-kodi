# -*- coding: utf-8 -*-
"""URL routing + Kodi list rendering.

Plugin URLs look like:
    plugin://plugin.video.grayjay/?action=home&source=<id>&page=<token>
    plugin://plugin.video.grayjay/?action=play&source=<id>&url=<contentUrl>
"""
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
        from .sources import manager
        from .engine.bridge import PluginBridge
        cfg = manager.get_source(source_id)
        if cfg is None:
            cache[source_id] = None
            return None
        bridge = PluginBridge(cfg)
        bridge.enable()
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
            items.append((
                self.url_for(action="home", source=cfg.id),
                cfg.name, True, cfg.icon_url,
            ))
        items.append((self.url_for(action="add_source"), "[ Add source… ]", False, ""))
        self._render(items)

    def action_add_source(self):
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
        xbmc.executebuiltin("Container.Refresh")

    # -- per-source feeds -------------------------------------------------
    def action_home(self):
        self._feed("getHome")

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
        """Aggregate recent content from every subscribed channel, across all
        sources, newest first. This is the Grayjay-style unified feed."""
        from .sources import subscriptions as subs
        all_subs = subs.list_subscriptions()
        if not all_subs:
            self._render([(self.url_for(action="root"),
                           "No subscriptions yet — use 'Subscribe' on any video", False, "")])
            return
        collected = []
        for s in all_subs:
            results = self._run(s["source"], "getChannelContents",
                                [s["url"], None, None, [], None])
            for v in results:
                collected.append((s["source"], v))
        # Newest first when items carry a datetime (unix seconds).
        collected.sort(key=lambda sv: (sv[1].get("datetime") or 0), reverse=True)
        items = [self._content_item(src, v) for src, v in collected]
        self._render(items, content_type="videos")

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
        else:
            items.append((self.url_for(action="subscribe", source=source_id, url=url, name=name),
                          "[ Subscribe ]", False, ""))
        for v in self._run(source_id, "getChannelContents", [url, None, None, [], None]):
            items.append(self._content_item(source_id, v))
        self._render(items, content_type="videos")

    def _content_item(self, source_id, v):
        title = v.get("name", "Untitled")
        url = self.url_for(action="play", source=source_id, url=v.get("url", ""))
        thumb = ""
        thumbs = (v.get("thumbnails") or {}).get("sources") or []
        if thumbs:
            thumb = thumbs[-1].get("url", "")
        return (url, title, False, thumb, v, self._context_menu(source_id, v))

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
        from .sources import manager
        from .engine.bridge import PluginBridge

        source_id = self.args.get("source")
        content_url = self.args.get("url")
        cfg = manager.get_source(source_id)
        if cfg is None:
            notify("Source not found")
            return
        bridge = PluginBridge(cfg)
        bridge.enable()
        details = bridge.call("getContentDetails", [content_url])
        play_url = self._pick_stream(details)
        if not play_url:
            notify("No playable stream found")
            return
        if _HAS_KODI:
            li = xbmcgui.ListItem(path=play_url)
            xbmcplugin.setResolvedUrl(self.handle, True, li)
        else:
            log("would play: %s" % play_url, "info")

    def _pick_stream(self, details):
        if not details:
            return None
        desc = details.get("video") or {}
        sources = desc.get("videoSources") or []
        # source.js tags the class via `plugin_type` (Grayjay), older shims via
        # `type`. Prefer adaptive HLS/DASH, then highest-res progressive.
        def kind(s):
            return s.get("plugin_type") or s.get("type") or ""
        for s in sources:
            k = kind(s)
            if "HLS" in k or "Dash" in k or "DASH" in k:
                return s.get("url")
        best, best_h = None, -1
        for s in sources:
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
            context = it[5] if len(it) > 5 else None
            li = xbmcgui.ListItem(label=label)
            if thumb:
                li.setArt({"thumb": thumb, "icon": thumb})
            if not is_folder:
                li.setProperty("IsPlayable", "true")
            if context:
                li.addContextMenuItems(context)
            xbmcplugin.addDirectoryItem(self.handle, url, li, is_folder)
        xbmcplugin.endOfDirectory(self.handle)
