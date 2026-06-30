# -*- coding: utf-8 -*-
"""Install, list, update and remove Grayjay sources.

A source lives in its own directory under <profile>/sources/<id>/ holding:
    config.json      - the SourceV8PluginConfig
    script.js        - the plugin code referenced by config.scriptUrl
    source.meta.json - host bookkeeping (install URL, last update check)

Installation downloads the config from a URL, then fetches its scriptUrl.
Updates re-fetch the config from its canonical update URL (see update_url)
and replace the files only when the remote version is newer AND the new
script's signature still verifies.

Security: Grayjay signs scripts (scriptSignature / scriptPublicKey). We verify
the signature against the exact downloaded bytes before persisting, on both
install and update — see config.SourceConfig.validate.
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

from urllib.parse import urljoin


_UA = "Mozilla/5.0 (compatible; grayjay-kodi/0.1; +https://github.com/grayjay-kodi)"

_META_NAME = "source.meta.json"


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


# -- install-meta sidecar -------------------------------------------------
# Kept out of config.json because we overwrite config.json verbatim from the
# remote on every update, which would clobber any host-injected field.
def _meta_path(base_dir):
    return os.path.join(base_dir, _META_NAME)


def read_meta(base_dir):
    try:
        with open(_meta_path(base_dir), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (IOError, OSError, ValueError):
        return {}


def write_meta(base_dir, **updates):
    meta = read_meta(base_dir)
    meta.update(updates)
    with open(_meta_path(base_dir), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
    return meta


# -- download / verify / persist (shared by install + update) -------------
def _resolve_script_url(raw, config_url):
    """Absolute scriptUrl, resolving a relative one against the config URL."""
    script_url = raw.get("scriptUrl")
    if not script_url:
        raise ValueError("config has no scriptUrl")
    if script_url.startswith("./") or not script_url.startswith("http"):
        script_url = urljoin(config_url, script_url)
    return script_url


def _download(config_url):
    """Fetch a source's config + script from a config URL.

    Returns (raw_config_dict, script_text). No disk writes — the caller
    verifies the signature before anything is persisted."""
    raw = json.loads(_fetch(config_url))
    script = _fetch(_resolve_script_url(raw, config_url))
    return raw, script


def _verify(raw, script, base_dir, source_id):
    """Verify the script signature for a (not-yet-persisted) config.

    Returns the reason string ("valid" / "unsigned"). Raises ValueError on an
    actively invalid signature so the caller aborts without touching disk."""
    cfg = SourceConfig(raw, base_dir)
    ok, reason = cfg.validate(script)
    if reason == "invalid":
        raise ValueError("Signature verification FAILED for %s" % source_id)
    if reason == "unsigned":
        log("source %s is UNSIGNED (security risk)" % source_id, "warning")
    else:
        log("Signature verified for %s" % source_id, "info")
    return reason


def _persist(base_dir, raw, script):
    """Write config.json + script.js into base_dir, preserving exact bytes."""
    if not os.path.isdir(base_dir):
        os.makedirs(base_dir)
    with open(os.path.join(base_dir, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(raw, fh, indent=2)
    # newline="" disables newline translation so the bytes (and any CRLF) are
    # preserved exactly — the signature is verified against these exact bytes.
    with open(os.path.join(base_dir, "script.js"), "w", encoding="utf-8", newline="") as fh:
        fh.write(script)


def _safe_id(raw):
    source_id = raw.get("id") or raw.get("name", "source")
    return "".join(c for c in source_id if c.isalnum() or c in "-_.")


# -- public API -----------------------------------------------------------
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
    """Download a source's config + script and persist it. Returns SourceConfig.

    The signature is verified *before* anything is written, so a failed verify
    never leaves a half-installed directory behind."""
    raw, script = _download(config_url)
    safe_id = _safe_id(raw)
    base_dir = os.path.join(sources_path(), safe_id)

    _verify(raw, script, base_dir, raw.get("id") or safe_id)
    _persist(base_dir, raw, script)
    # Remember where we installed from so updates have a fallback when the
    # config omits sourceUrl. Prefer sourceUrl (Grayjay convention) at update
    # time; install_url is the safety net.
    write_meta(base_dir, install_url=config_url)

    cfg = SourceConfig.from_dir(base_dir)
    log("installed source %s v%s" % (cfg.id, cfg.version), "info")
    notify("Installed %s" % cfg.name)
    return cfg


def remove_source(source_id):
    d = os.path.join(sources_path(), source_id)
    if os.path.isdir(d):
        shutil.rmtree(d)
        notify("Removed %s" % source_id)
        return True
    return False
