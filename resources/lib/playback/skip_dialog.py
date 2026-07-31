# -*- coding: utf-8 -*-
"""Non-blocking OSD overlay dialog for SponsorBlock segment skip prompts.

Shows a "Skip Segment (5s)" button in the bottom-right corner of the screen
while a Manual-mode SponsorBlock segment is playing.  The dialog is
non-blocking — the user can watch the video and choose to click Skip or just
let it pass.  It auto-closes when the segment ends.
"""
try:
    import xbmcgui
    _HAS_KODI = True
except ImportError:
    _HAS_KODI = False
    xbmcgui = None

from ..kodiutils import log, addon_path

# Control IDs (must match script-sponsorblock-skip.xml)
_SKIP_BUTTON = 3012
_CLOSE_BUTTON = 3013


def _format_duration(seconds):
    """Format seconds as '1m 40s' or '25s'."""
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes > 0:
        return "%dm %ds" % (minutes, secs)
    return "%ds" % secs


if _HAS_KODI:
    class SkipDialog(xbmcgui.WindowXMLDialog):
        """Non-blocking OSD overlay with a Skip button for Manual segments."""

        def __init__(self, *args, **kwargs):
            self._segment_category = kwargs.pop("category", "segment")
            self._duration = kwargs.pop("duration", 0)
            self.skip_requested = False
            self.cancel_requested = False
            xbmcgui.WindowXMLDialog.__init__(self, *args)

        def set_skip_info(self, category, duration):
            self._segment_category = category
            self._duration = duration
            label = "Skip %s (%s)" % (
                category, _format_duration(duration))
            self.setProperty("skip_label", label)

        def onInit(self):
            try:
                btn = self.getControl(_SKIP_BUTTON)
                label = self.getProperty("skip_label")
                if label:
                    btn.setLabel(label)
            except Exception:
                pass

        def onAction(self, action):
            aid = action.getId()
            if aid in (9, 10, 92):  # back / prev menu / nav back
                self.cancel_requested = True
                self.close()

        def onClick(self, control_id):
            if control_id == _SKIP_BUTTON:
                self.skip_requested = True
                self.close()
            elif control_id == _CLOSE_BUTTON:
                self.cancel_requested = True
                self.close()

        def is_skip(self):
            return self.skip_requested

        def is_cancel(self):
            return self.cancel_requested


def show_skip_dialog(category, duration):
    """Create and show the skip dialog.  Returns the dialog instance, or None."""
    if not _HAS_KODI:
        return None
    try:
        dlg = SkipDialog(
            "script-sponsorblock-skip.xml",
            addon_path(),
            "default",
            "1080i",
        )
        dlg.set_skip_info(category, duration)
        dlg.show()
        return dlg
    except Exception as exc:
        log("sponsorblock: failed to show skip dialog: %s" % exc, "warning")
        return None


def close_skip_dialog(dlg):
    """Safely close a skip dialog."""
    if dlg is None:
        return
    try:
        dlg.close()
    except Exception:
        pass
