# -*- coding: utf-8 -*-
"""Install, list and remove Grayjay sources.

A source lives in its own directory under <profile>/sources/<id>/ holding:
    config.json   - the SourceV8PluginConfig
    script.js     - the plugin code referenced by config.scriptUrl

Installation downloads the config from a URL, then fetches its scriptUrl.

Security TODO: Grayjay signs scripts (scriptSignature / scriptPublicKey).
We do not yet verify signatures — see README "Security".
"""
import json
import os
import shutil

from ..kodiutils import sources_path, log, notify
from .config import SourceConfig

try:
    import requests as _requests
except ImportError:
    _requests = None
import urllib.request as _urlreq


_UA = "Mozilla/5.0 (compatible; grayjay-kodi/0.1; +https://github.com/grayjay-kodi)"


def _fetch(url):
    """Fetch text as UTF-8. Decoding must be exact (and stable) because the
    script bytes are what the plugin signature is verified against."""
    if _requests is not None:
        r = _requests.get(url, timeout=20, headers={"User-Agent": _UA})
        r.raise_for_status()
        return r.content.decode("utf-8")
    req = _urlreq.Request(url, headers={"User-Agent": _UA})
    with _urlreq.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8")


def list_sources():
    """Return installed SourceConfig objects."""
    out = []
    root = sources_path()
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if os.path.isfile(os.path.join(d, "config.json")):
            try:
                out.append(SourceConfig.from_dir(d))
            except Exception as exc:
                log("skipping bad source %s: %s" % (name, exc), "warning")
    return out


def get_source(source_id):
    d = os.path.join(sources_path(), source_id)
    if os.path.isfile(os.path.join(d, "config.json")):
        return SourceConfig.from_dir(d)
    return None


def install_from_url(config_url):
    """Download a source's config + script and persist it. Returns SourceConfig."""
    raw = json.loads(_fetch(config_url))
    source_id = raw.get("id") or raw.get("name", "source")
    safe_id = "".join(c for c in source_id if c.isalnum() or c in "-_.")
    base_dir = os.path.join(sources_path(), safe_id)
    if not os.path.isdir(base_dir):
        os.makedirs(base_dir)

    script_url = raw.get("scriptUrl")
    if not script_url:
        raise ValueError("config has no scriptUrl")
    # Resolve relative scriptUrl against the config URL.
    if script_url.startswith("./") or not script_url.startswith("http"):
        from urllib.parse import urljoin
        script_url = urljoin(config_url, script_url)

    script = _fetch(script_url)
    with open(os.path.join(base_dir, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(raw, fh, indent=2)
    # newline="" disables newline translation so the bytes (and any CRLF) are
    # preserved exactly — the signature is verified against these exact bytes.
    with open(os.path.join(base_dir, "script.js"), "w", encoding="utf-8", newline="") as fh:
        fh.write(script)

    # Verify the signature at install time (Grayjay SignatureProvider).
    cfg = SourceConfig.from_dir(base_dir)
    ok, reason = cfg.validate(script)
    if reason == "invalid":
        shutil.rmtree(base_dir)
        raise ValueError("Signature verification FAILED for %s — not installed" % source_id)
    if reason == "unsigned":
        log("Installed UNSIGNED source %s (security risk)" % source_id, "warning")
    else:
        log("Signature verified for %s" % source_id, "info")

    log("installed source %s v%s" % (source_id, raw.get("version")), "info")
    notify("Installed %s" % raw.get("name", source_id))
    return cfg


def remove_source(source_id):
    d = os.path.join(sources_path(), source_id)
    if os.path.isdir(d):
        shutil.rmtree(d)
        notify("Removed %s" % source_id)
        return True
    return False
