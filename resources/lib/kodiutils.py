# -*- coding: utf-8 -*-
"""Thin wrappers around Kodi's xbmc* modules.

Kept in one place so the rest of the addon doesn't sprinkle xbmc calls around,
and so non-Kodi tooling (tests, the standalone harness) can stub a single
module.
"""
import os

try:
    import xbmc
    import xbmcaddon
    import xbmcvfs
    _HAS_KODI = True
except ImportError:  # running outside Kodi (tests / dev harness)
    _HAS_KODI = False
    xbmc = xbmcaddon = xbmcvfs = None

ADDON_ID = "plugin.video.grayjay"


def _addon():
    return xbmcaddon.Addon(ADDON_ID)


def log(msg, level="info"):
    levels = {
        "debug": getattr(xbmc, "LOGDEBUG", 0) if _HAS_KODI else 0,
        "info": getattr(xbmc, "LOGINFO", 1) if _HAS_KODI else 1,
        "warning": getattr(xbmc, "LOGWARNING", 2) if _HAS_KODI else 2,
        "error": getattr(xbmc, "LOGERROR", 3) if _HAS_KODI else 3,
    }
    line = "[%s] %s" % (ADDON_ID, msg)
    if _HAS_KODI:
        xbmc.log(line, levels.get(level, 1))
    else:
        print(line)


def get_setting(key, default=""):
    if _HAS_KODI:
        val = _addon().getSetting(key)
        return val if val != "" else default
    return os.environ.get("GRAYJAY_%s" % key.upper(), default)


def set_setting(key, value):
    if _HAS_KODI:
        _addon().setSetting(key, str(value))


def addon_path():
    """Installed addon directory (read-only resources)."""
    if _HAS_KODI:
        return xbmcvfs.translatePath(_addon().getAddonInfo("path"))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def profile_path():
    """Writable per-user data directory (installed sources, state, cache)."""
    if _HAS_KODI:
        path = xbmcvfs.translatePath(_addon().getAddonInfo("profile"))
    else:
        path = os.path.join(
            os.path.expanduser("~"), ".grayjay-kodi"
        )
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


def sources_path():
    path = os.path.join(profile_path(), "sources")
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


def resolve_ca_bundle():
    """Pick a CA bundle that can actually build trust chains on this box.

    Kodi ships `script.module.certifi`, and `requests` uses it by default — but
    that bundle lags the OS trust store and fails to verify hosts whose root CA
    is newer than the bundle (e.g. ted.com → 'unable to get local issuer
    certificate'). The CoreELEC/OS store is kept current, so prefer it. Honor
    the standard env overrides first; fall back to requests' own default (True)
    when nothing concrete is found."""
    for env in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        p = os.environ.get(env)
        if p and os.path.isfile(p):
            return p
    for p in ("/etc/ssl/cert.pem",                       # CoreELEC / *BSD / macOS
              "/etc/ssl/certs/ca-certificates.crt",      # Debian/Ubuntu
              "/etc/pki/tls/certs/ca-bundle.crt"):       # Fedora/RHEL
        if os.path.isfile(p):
            return p
    return True  # let requests use its bundled certifi default


def notify(message, heading="Grayjay"):
    if _HAS_KODI:
        import xbmcgui
        xbmcgui.Dialog().notification(heading, message)
    else:
        print("NOTIFY: %s - %s" % (heading, message))
