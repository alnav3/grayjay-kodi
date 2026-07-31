# -*- coding: utf-8 -*-
"""Player monitor: watch-state tracking, SponsorBlock segment skipping,
and "Up next" autoplay.

Runs inside the background service. The plugin process can't observe playback
(it exits right after setResolvedUrl), so the router leaves a now_playing.json
handoff; this monitor claims it when playback starts, polls the player
position while the video plays (Kodi reports no position in
onPlayBackStopped, so polling is the only way to know where the user left
off), and finalises the entry — History or On Deck — when playback ends.
A natural end also triggers "Up next": the following item from the directory
the video was started from, played silently (no on-screen notification --
just the next video starting).

When a YouTube video is claimed and SponsorBlock category settings are
provided in the handoff, the monitor fetches community-submitted sponsor
segments and automatically seeks past "Automatic" ones, or prompts the user
for "Manual" ones.
"""
import time

import xbmc

from ..kodiutils import log, get_setting
from .. import watch
from . import sponsorblock
from .skip_dialog import show_skip_dialog, close_skip_dialog

_SAVE_EVERY = 15        # seconds between watch-state writes while playing
# A stream that dies mid-video can still fire onPlayBackEnded; only count an
# "ended" playback as watched when it actually got near the end.
_ENDED_WATCHED_FRACTION = 0.8

# Minimum gap (seconds) between the current position and a segment's start
# for us to consider skipping it.  This avoids re-prompting / re-skipping
# when the player lands inside a segment after a seek (e.g. the user seeked
# past the end but the player reports a position a few seconds before).
_SEGMENT_GUARD = 2.0


def _autoplay_enabled():
    return get_setting("autoplay_next", "true") == "true"


class PlayerMonitor(xbmc.Player):
    def __init__(self):
        xbmc.Player.__init__(self)
        self._current = None    # claimed now-playing handoff, or None
        self._pos = 0.0
        self._total = 0.0
        self._last_save = 0.0
        # SponsorBlock state
        self._sb_segments = []          # fetched segments for current video
        self._sb_categories = {}        # {api_category: action}
        self._sb_last_skipped = None    # uuid of last auto-skipped segment
        self._sb_dialog = None          # active SkipDialog OSD, or None
        self._sb_dialog_end = 0.0       # segment end time for active dialog

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
        """Poll position while a tracked video plays; call every few seconds.

        Also checks SponsorBlock segments: auto-seeks past "Automatic" segments
        and offers a skip prompt for "Manual" ones."""
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
        self._sb_tick(pos)
        now = time.time()
        if self._total > 0 and now - self._last_save >= _SAVE_EVERY:
            self._last_save = now
            watch.record_progress(cur.get("source"), cur.get("url"),
                                  cur.get("name", ""), cur.get("thumbnail", ""),
                                  self._pos, self._total)

    # -- internals ------------------------------------------------------------
    def _claim(self):
        np = watch.claim_now_playing()
        if not np:
            return
        self._current = np
        self._pos = 0.0
        self._total = float(np.get("duration") or 0)
        self._last_save = time.time()
        log("tracking playback: %s" % (np.get("name") or np.get("url")), "info")
        self._sb_init(np)

    def _finalize(self, ended):
        cur, self._current = self._current, None
        if not cur:
            return
        pos, total = self._pos, self._total
        self._pos = self._total = 0.0
        # Clear SponsorBlock state for this video.
        self._sb_segments = []
        self._sb_categories = {}
        self._sb_last_skipped = None
        close_skip_dialog(self._sb_dialog)
        self._sb_dialog = None
        self._sb_dialog_end = 0.0
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
                xbmc.executebuiltin('PlayMedia("%s")' % nxt["play"])

    # -- SponsorBlock --------------------------------------------------------
    def _sb_init(self, now_playing):
        """Load SponsorBlock segments for the current video, if applicable.

        The now_playing handoff carries ``sb_categories`` when the source has
        SponsorBlock settings configured.  We extract the video ID from the
        content URL and fetch segments from the SponsorBlock API."""
        self._sb_segments = []
        self._sb_categories = {}
        self._sb_last_skipped = None
        cats = now_playing.get("sb_categories")
        if not cats:
            return
        content_url = now_playing.get("url", "")
        video_id = sponsorblock._resolve_video_id(content_url)
        if not video_id:
            return
        self._sb_categories = cats
        try:
            self._sb_segments = sponsorblock.fetch_segments(
                video_id, categories=set(cats.keys()))
        except Exception as exc:
            log("sponsorblock: failed to load segments for %s: %s"
                % (video_id, exc), "debug")
            self._sb_segments = []
        if self._sb_segments:
            log("sponsorblock: %d segment(s) for %s"
                % (len(self._sb_segments), video_id), "debug")

    def _sb_tick(self, position):
        """Check current playback position against SponsorBlock segments.

        Auto-seeks past "Automatic" segments.  For "Manual" segments, shows a
        non-blocking OSD skip button that the user can click or ignore.  The
        dialog auto-closes when the segment ends."""
        # --- monitor an active skip dialog ---
        if self._sb_dialog is not None:
            # Auto-close when we've passed the segment end.
            if position and position >= self._sb_dialog_end:
                close_skip_dialog(self._sb_dialog)
                self._sb_dialog = None
                self._sb_dialog_end = 0.0
            elif self._sb_dialog.is_skip():
                target = self._sb_dialog_end
                close_skip_dialog(self._sb_dialog)
                self._sb_dialog = None
                self._sb_dialog_end = 0.0
                log("sponsorblock: user skipped to %.1f" % target, "info")
                try:
                    self.seekTime(target)
                except RuntimeError:
                    pass
            elif self._sb_dialog.is_cancel():
                close_skip_dialog(self._sb_dialog)
                self._sb_dialog = None
                self._sb_dialog_end = 0.0
            return  # don't check for new segments while dialog is open

        # --- check for a new segment to handle ---
        if not self._sb_segments or not self._sb_categories:
            return
        if not position or position <= 0:
            return
        action, seg = sponsorblock.check_position(
            position, self._sb_segments, self._sb_categories)
        if action == sponsorblock.ACTION_NONE:
            return
        # Avoid re-triggering for the same segment.
        uuid = seg.get("uuid")
        if uuid == self._sb_last_skipped:
            return
        # Guard: don't trigger a segment whose start is far in the future.
        if seg["start"] > position + _SEGMENT_GUARD:
            return
        if action == sponsorblock.ACTION_SKIP:
            target = seg["end"]
            self._sb_last_skipped = uuid
            log("sponsorblock: auto-skip %s [%.1f-%.1f] -> %.1f"
                % (seg.get("category", ""), seg["start"], seg["end"], target),
                "info")
            try:
                self.seekTime(target)
            except RuntimeError:
                pass
        elif action == sponsorblock.ACTION_MANUAL:
            self._sb_last_skipped = uuid
            duration = seg["end"] - seg["start"]
            log("sponsorblock: showing skip button for %s [%.1f-%.1f]"
                % (seg.get("category", ""), seg["start"], seg["end"]),
                "debug")
            self._sb_dialog = show_skip_dialog(
                seg.get("category", "segment"), duration)
            self._sb_dialog_end = seg["end"]
