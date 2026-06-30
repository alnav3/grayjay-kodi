# -*- coding: utf-8 -*-
"""Parsing for a Grayjay source's config JSON (SourceV8PluginConfig)."""
import json
import os


class SourceConfig(object):
    def __init__(self, raw, base_dir):
        self.raw = raw
        self.base_dir = base_dir

    # -- convenience accessors -------------------------------------------
    @property
    def id(self):
        return self.raw.get("id") or self.raw.get("name", "unknown")

    @property
    def name(self):
        return self.raw.get("name", "Unknown source")

    @property
    def author(self):
        return self.raw.get("author", "")

    @property
    def version(self):
        return self.raw.get("version", 0)

    @property
    def icon_url(self):
        return self.raw.get("iconUrl", "")

    @property
    def script_url(self):
        return self.raw.get("scriptUrl", "")

    @property
    def update_url(self):
        """Canonical config URL to re-fetch this source from when checking for
        updates. Grayjay publishes this as `sourceUrl`; fall back to the URL we
        originally installed from (recorded in the host meta sidecar)."""
        src = self.raw.get("sourceUrl")
        if src:
            return src
        try:
            from . import manager
            return manager.read_meta(self.base_dir).get("install_url", "")
        except Exception:
            return ""

    @property
    def script_path(self):
        """Local path to the downloaded plugin .js for this source."""
        return os.path.join(self.base_dir, "script.js")

    @property
    def script_signature(self):
        return self.raw.get("scriptSignature")

    @property
    def script_public_key(self):
        return self.raw.get("scriptPublicKey")

    def validate(self, script_text):
        """Mirror Grayjay's SourcePluginConfig.validate / SignatureProvider.

        Returns (ok, reason). `ok` is True only when a signature is present
        and verifies. `reason` is "unsigned" when signing fields are absent
        (caller decides whether to allow), or "invalid" on a bad signature.
        """
        if not self.script_public_key:
            return False, "unsigned"
        if not self.script_signature:
            return False, "unsigned"
        from ..crypto.rsa_verify import verify
        ok = verify(script_text, self.script_signature, self.script_public_key)
        return (ok, "valid" if ok else "invalid")

    @property
    def allow_eval(self):
        return bool(self.raw.get("allowEval", False))

    @property
    def allow_urls(self):
        return self.raw.get("allowUrls", []) or []

    def url_allowed(self, url):
        """Honor the plugin's declared allowUrls (['everywhere'] = no limit)."""
        allow = self.allow_urls
        if not allow or "everywhere" in allow:
            return True
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        for entry in allow:
            entry = entry.lower().lstrip("*.")
            if host == entry or host.endswith("." + entry):
                return True
        return False

    @classmethod
    def from_dir(cls, base_dir):
        cfg_path = os.path.join(base_dir, "config.json")
        with open(cfg_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return cls(raw, base_dir)
