# -*- coding: utf-8 -*-
"""Player monitor: watch-state tracking + "Up next" autoplay.

Runs inside the background service. The plugin process can't observe playback
(it exits right after setResolvedUrl), so the router leaves a now_playing.json
handoff; this monitor claims it when playback starts, polls the player
position while the video plays (Kodi reports no position in
onPlayBackStopped, so polling is the only way to know where the user left
off), and finalises the entry — History or On Deck — when playback ends.
A natural end also triggers "Up next": the following item from the directory
the video was started from.
"""
import time

import xbmc

from ..kodiutils import log, notify, get_setting
from .. import watch

_SAVE_EVERY = 15        # seconds between watch-state writes while playing
_NEAR_END = 30          # seconds before the end to announce what's next
# A stream that dies mid-video can still fire onPlayBackEnded; only count an
# "ended" playback as watched when it actually got near the end.
_ENDED_WATCHED_FRACTION = 0.8


def _autoplay_enabled():
    return get_setting("autoplay_next", "true") == "true"


class PlayerMonitor(xbmc.Player):
    def __init__(self):
        xbmc.Player.__init__(self)
        self._current = None    # claimed now-playing handoff, or None
        self._pos = 0.0
        self._total = 0.0
        self._last_save = 0.0
        self._announced = False

    # -- Kodi callbacks (fire on Kodi's threads) ---------------------------
    def onAVStarted(self):
        self._claim()

    def onPlayBackStarted(self):
        # Kodi 18+: fires when playback is requested, before AV renders.
        # Claiming here too just means an earlier claim; a start that then
        # fails is finalised at position 0, which is a no-op.
        if self._current is None:
            self._claim()

    def onPlayBackEnded(self):
        self._finalize(ended=True)

    def onPlayBackStopped(self):
        self._finalize(ended=False)

    def onPlayBackError(self):
        self._finalize(ended=False)

    # -- service loop -------------------------------------------------------
    def tick(self):
        """Poll position while a tracked video plays; call every few seconds."""
        cur = self._current
        if not cur:
            return
        try:
            if not self.isPlayingVideo():
                return
            pos = self.getTime()
            total = self.getTotalTime()
        except RuntimeError:
            return
        if pos and pos > 0:
            self._pos = pos
        if total and total > 0:
            self._total = total
        now = time.time()
        if self._total > 0 and now - self._last_save >= _SAVE_EVERY:
            self._last_save = now
            watch.record_progress(cur.get("source"), cur.get("url"),
                                  cur.get("name", ""), cur.get("thumbnail", ""),
                                  self._pos, self._total)
        if (not self._announced and self._total > 0
                and (self._total - self._pos) <= _NEAR_END):
            self._announced = True
            nxt = self._next_item()
            if nxt and _autoplay_enabled():
                notify("Up next: %s" % (nxt.get("name") or "next video"))

    # -- internals ------------------------------------------------------------
    def _claim(self):
        np = watch.claim_now_playing()
        if not np:
            return
        self._current = np
        self._pos = 0.0
        self._total = float(np.get("duration") or 0)
        self._last_save = time.time()
        self._announced = False
        log("tracking playback: %s" % (np.get("name") or np.get("url")), "info")

    def _next_item(self):
        cur = self._current or {}
        qid = cur.get("qid")
        if not qid:
            return None
        try:
            idx = int(cur.get("idx"))
        except (TypeError, ValueError):
            return None
        return watch.queue_item(qid, idx + 1)

    def _finalize(self, ended):
        cur, self._current = self._current, None
        if not cur:
            return
        pos, total = self._pos, self._total
        self._pos = self._total = 0.0
        if total > 0:
            watched = (pos / total) >= (_ENDED_WATCHED_FRACTION if ended
                                        else watch.WATCHED_FRACTION)
        else:
            watched = ended
        if watched:
            watch.mark_watched(cur.get("source"), cur.get("url"),
                               cur.get("name", ""), cur.get("thumbnail", ""),
                               total or cur.get("duration") or 0)
        else:
            watch.record_progress(cur.get("source"), cur.get("url"),
                                  cur.get("name", ""), cur.get("thumbnail", ""),
                                  pos, total or cur.get("duration") or 0)
        if ended and watched and _autoplay_enabled():
            nxt = None
            qid, idx = cur.get("qid"), cur.get("idx")
            if qid:
                try:
                    nxt = watch.queue_item(qid, int(idx) + 1)
                except (TypeError, ValueError):
                    nxt = None
            if nxt and nxt.get("play"):
                log("up next: %s" % (nxt.get("name") or nxt["play"]), "info")
                notify("Up next: %s" % (nxt.get("name") or "next video"))
                xbmc.executebuiltin('PlayMedia("%s")' % nxt["play"])
