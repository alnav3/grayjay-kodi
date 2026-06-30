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

    # -- top level --------------------------------------------------------
    def action_root(self):
        """List installed sources + management entries."""
        from .sources import manager
        items = []
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
        from .sources import manager
        from .engine.bridge import PluginBridge

        source_id = self.args.get("source")
        page = self.args.get("page") or None
        cfg = manager.get_source(source_id)
        if cfg is None:
            notify("Source not found: %s" % source_id)
            return

        bridge = PluginBridge(cfg)
        bridge.enable()
        call_args = (extra_args or []) + [page]
        result = bridge.call(method, call_args)
        results = (result or {}).get("results", []) if isinstance(result, dict) else (result or [])

        items = []
        for v in results:
            items.append(self._content_item(source_id, v))
        self._render(items, content_type="videos")

    def _content_item(self, source_id, v):
        title = v.get("name", "Untitled")
        url = self.url_for(action="play", source=source_id, url=v.get("url", ""))
        thumb = ""
        thumbs = (v.get("thumbnails") or {}).get("sources") or []
        if thumbs:
            thumb = thumbs[-1].get("url", "")
        return (url, title, False, thumb, v)

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
            li = xbmcgui.ListItem(label=label)
            if thumb:
                li.setArt({"thumb": thumb, "icon": thumb})
            if not is_folder:
                li.setProperty("IsPlayable", "true")
            xbmcplugin.addDirectoryItem(self.handle, url, li, is_folder)
        xbmcplugin.endOfDirectory(self.handle)
