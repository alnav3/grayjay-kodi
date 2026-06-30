# -*- coding: utf-8 -*-
"""Auto-update for installed Grayjay sources.

Grayjay sources carry an integer `version` and a canonical `sourceUrl`. An
update is: re-fetch the config from that URL, and if its version is newer than
what's installed, download the new script, verify its signature, and atomically
swap the files in place. A failed fetch or a bad signature leaves the working
copy untouched.

This module is engine-agnostic and Kodi-agnostic so it runs under the test
harness too; the background trigger lives in service.py.
"""
import os

from ..kodiutils import log, notify, get_setting
from . import manager
from .config import SourceConfig


def _version_tuple(v):
    """Comparable key for a source version.

    Grayjay versions are integers, but be lenient: a dotted string like
    "1.2.3" compares component-wise, anything else falls back to a string."""
    if isinstance(v, int):
        return (1, (v,))
    if isinstance(v, float):
        return (1, (int(v),))
    s = str(v or "").strip()
    try:
        return (1, tuple(int(p) for p in s.split(".")))
    except ValueError:
        return (0, (s,))


def is_newer(remote, local):
    """True if `remote` version is strictly newer than `local`."""
    return _version_tuple(remote) > _version_tuple(local)


def check_source(cfg):
    """Look up whether a newer version of `cfg` is published upstream.

    Returns a dict {source, name, installed, available, has_update, error}.
    Never raises — network/parse failures surface as `error`."""
    info = {
        "source": cfg.id,
        "name": cfg.name,
        "installed": cfg.version,
        "available": None,
        "has_update": False,
        "error": None,
    }
    url = cfg.update_url
    if not url:
        info["error"] = "no update URL"
        return info
    try:
        import json
        raw = json.loads(manager._fetch(url))
    except Exception as exc:
        info["error"] = str(exc)
        log("update check failed for %s: %s" % (cfg.id, exc), "warning")
        return info
    remote_version = raw.get("version", 0)
    info["available"] = remote_version
    info["has_update"] = is_newer(remote_version, cfg.version)
    manager.write_meta(cfg.base_dir, last_check_version=remote_version)
    return info


def update_source(cfg):
    """Download and apply the newest version of `cfg` if one exists.

    Returns (applied, info) where `info` is the check_source dict. The new
    script's signature is verified before any file is overwritten, so a bad
    update can't break a working source."""
    info = check_source(cfg)
    if info["error"] or not info["has_update"]:
        return False, info

    url = cfg.update_url
    try:
        raw, script = manager._download(url)
    except Exception as exc:
        info["error"] = str(exc)
        log("update download failed for %s: %s" % (cfg.id, exc), "warning")
        return False, info

    # Re-confirm the freshly downloaded config is actually newer (it may have
    # changed between the check and the download) before touching disk.
    if not is_newer(raw.get("version", 0), cfg.version):
        info["has_update"] = False
        return False, info

    try:
        manager._verify(raw, script, cfg.base_dir, cfg.id)
    except ValueError as exc:
        # Bad signature — keep the working copy, report loudly.
        info["error"] = str(exc)
        log("update REJECTED for %s: %s" % (cfg.id, exc), "error")
        notify("Update blocked: bad signature for %s" % cfg.name)
        return False, info

    manager._persist(cfg.base_dir, raw, script)
    new_version = raw.get("version", 0)
    info["installed"] = new_version
    info["available"] = new_version
    info["applied_version"] = new_version
    log("updated %s -> v%s" % (cfg.id, new_version), "info")
    return True, info


def check_all():
    """Check every installed source. Returns a list of check_source dicts."""
    return [check_source(cfg) for cfg in manager.list_sources()]


def update_all(notify_summary=True):
    """Apply available updates to every installed source.

    Returns (updated, checked) lists of info dicts. Sources are re-loaded fresh
    so each carries its current on-disk version."""
    updated, checked = [], []
    for cfg in manager.list_sources():
        applied, info = update_source(cfg)
        checked.append(info)
        if applied:
            updated.append(info)
    if notify_summary and updated:
        names = ", ".join(u["name"] for u in updated)
        notify("Updated %d source(s): %s" % (len(updated), names))
    elif notify_summary and not any(c["error"] for c in checked):
        log("source update check: all %d up to date" % len(checked), "info")
    return updated, checked


# -- settings-driven entry point (used by the service + menu) -------------
def auto_update_enabled():
    return get_setting("auto_update", "true") in ("true", "True", "1", True)


def update_interval_hours():
    try:
        return max(1, int(get_setting("update_interval_hours", "24")))
    except (TypeError, ValueError):
        return 24
