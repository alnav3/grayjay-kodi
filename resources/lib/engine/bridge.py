# -*- coding: utf-8 -*-
"""Host bridge: wires Python I/O into the JS engine and drives a plugin.

Lifecycle:
    bridge = PluginBridge(config)
    bridge.load()                      # eval packages.js + plugin script
    bridge.enable()                    # call source.enable(conf, settings, state)
    pager = bridge.call("getHome", [None])
"""
import base64
import hashlib
import json
import os
import uuid

from ..kodiutils import log
from .jsengine import JSEngine

try:
    import requests as _requests
except ImportError:
    _requests = None

import urllib.request as _urlreq
import urllib.error  # noqa: F401  (exposes _urlreq.HTTPError reliably)


_DIR = os.path.dirname(os.path.abspath(__file__))
HOST_PRELUDE_JS = os.path.join(_DIR, "host_prelude.js")  # host-injected packages
SOURCE_JS = os.path.join(_DIR, "source.js")              # Grayjay's own SDK prelude
DOM_JS = os.path.join(_DIR, "dom.js")                    # domParser package


class SignatureError(Exception):
    pass


class PluginBridge(object):
    def __init__(self, config):
        self.config = config           # sources.config.SourceConfig
        self.engine = JSEngine()
        self._loaded = False
        from .dom import DOMRegistry
        self._dom = DOMRegistry()

    # -- host callables ---------------------------------------------------
    def _host_log(self, payload_json):
        try:
            data = json.loads(payload_json)
            log("[plugin:%s] %s" % (self.config.id, data.get("msg")), "debug")
        except Exception:
            pass
        return None

    def _host_http(self, payload_json):
        data = json.loads(payload_json)
        method = (data.get("method") or "GET").upper()
        url = data.get("url")
        headers = data.get("headers") or {}
        body = data.get("body")
        # allowUrls enforcement (basic): if config restricts domains, honor it.
        if not self.config.url_allowed(url):
            return json.dumps({"url": url, "code": 0, "headers": {}, "body": "",
                               "error": "URL blocked by plugin allowUrls"})
        # Ensure a browser-like UA unless the plugin set one (many sites 403 the
        # default urllib/python agent).
        if not any(k.lower() == "user-agent" for k in headers):
            headers["User-Agent"] = self._default_ua()
        try:
            if _requests is not None:
                resp = _requests.request(method, url, headers=headers,
                                         data=body, timeout=20, allow_redirects=True)
                return json.dumps({
                    "url": resp.url, "code": resp.status_code,
                    "headers": dict(resp.headers), "body": resp.text,
                })
            req = _urlreq.Request(url, method=method, headers=headers,
                                  data=body.encode("utf-8") if body else None)
            try:
                r = _urlreq.urlopen(req, timeout=20)
            except _urlreq.HTTPError as he:
                # Non-2xx: return the response rather than raising, so the
                # plugin can inspect status/body (e.g. to detect captchas).
                body_txt = he.read().decode("utf-8", "replace") if hasattr(he, "read") else ""
                return json.dumps({"url": url, "code": he.code,
                                   "headers": dict(he.headers or {}), "body": body_txt})
            with r:
                raw = r.read().decode("utf-8", "replace")
                return json.dumps({"url": r.geturl(), "code": r.status,
                                   "headers": dict(r.headers), "body": raw})
        except Exception as exc:
            log("http error %s: %s" % (url, exc), "warning")
            return json.dumps({"url": url, "code": 0, "headers": {}, "body": "", "error": str(exc)})

    @staticmethod
    def _default_ua():
        return ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36")

    def _host_b64encode(self, payload_json):
        data = json.loads(payload_json).get("data", "")
        return json.dumps({"out": base64.b64encode(data.encode("utf-8")).decode("ascii")})

    def _host_b64decode(self, payload_json):
        data = json.loads(payload_json).get("data", "")
        return json.dumps({"out": base64.b64decode(data).decode("utf-8", "replace")})

    def _host_uuid(self, payload_json):
        return json.dumps({"out": str(uuid.uuid4())})

    def _host_md5(self, payload_json):
        data = json.loads(payload_json).get("data", "")
        return json.dumps({"out": hashlib.md5(data.encode("utf-8")).hexdigest()})

    def _host_toast(self, payload_json):
        try:
            from ..kodiutils import notify
            notify(json.loads(payload_json).get("msg", ""))
        except Exception:
            pass
        return None

    def _host_sleep(self, payload_json):
        import time
        try:
            ms = float(json.loads(payload_json).get("ms", 0))
            time.sleep(min(max(ms, 0) / 1000.0, 10.0))  # cap at 10s
        except Exception:
            pass
        return None

    def _register_host(self):
        e = self.engine
        e.register("__host_log", self._host_log)
        e.register("__host_http", self._host_http)
        e.register("__host_b64encode", self._host_b64encode)
        e.register("__host_b64decode", self._host_b64decode)
        e.register("__host_uuid", self._host_uuid)
        e.register("__host_md5", self._host_md5)
        e.register("__host_dom_parse", self._dom.parse)
        e.register("__host_dom_op", self._dom.op)
        e.register("__host_toast", self._host_toast)
        e.register("__host_sleep", self._host_sleep)

    # -- lifecycle --------------------------------------------------------
    def load(self):
        if self._loaded:
            return
        # newline="" preserves exact bytes (incl. CRLF) so signature checks
        # against the same content that was downloaded and signed.
        with open(self.config.script_path, "r", encoding="utf-8", newline="") as fh:
            script = fh.read()

        # Signature verification (Grayjay SignatureProvider, SHA512withRSA).
        ok, reason = self.config.validate(script)
        from ..kodiutils import get_setting
        require = get_setting("verify_signatures", "false") == "true"
        if reason == "invalid":
            raise SignatureError(
                "Plugin %s has an INVALID signature — refusing to load."
                % self.config.id
            )
        if reason == "unsigned":
            msg = "Plugin %s is unsigned." % self.config.id
            if require:
                raise SignatureError(
                    msg + " 'Require signatures' is on — refusing to load."
                )
            log(msg + " Running unsigned (security risk).", "warning")
        else:
            log("Plugin %s signature verified." % self.config.id, "info")

        self._register_host()
        # host_prelude/dom are IIFEs that attach to globalThis, so they can be
        # eval'd independently. source.js + the plugin, however, use top-level
        # let/const/class — those are lexically scoped to a single eval and are
        # invisible across eval calls. Grayjay runs them as one compilation
        # unit, so we concatenate source.js + config + plugin and expose the
        # resulting `source`/`plugin`/`Type` onto globalThis for later calls.
        for path in (HOST_PRELUDE_JS, DOM_JS):
            with open(path, "r", encoding="utf-8") as fh:
                self.engine.eval(fh.read())
        with open(SOURCE_JS, "r", encoding="utf-8") as fh:
            source_sdk = fh.read()
        # Apply engine-specific fixups (e.g. quickjs \- in /u classes) to the
        # SDK and plugin code. Signature was verified above on the original
        # bytes; this only adapts the code for our JS engine.
        combined = "\n;\n".join([
            self.engine.prepare(source_sdk),
            "plugin.config = %s; plugin.settings = plugin.settings || {};" % json.dumps(self.config.raw),
            self.engine.prepare(script),
            "globalThis.source = source; globalThis.plugin = plugin; globalThis.Type = Type;",
        ])
        self.engine.eval(combined)
        self._loaded = True

    def enable(self, settings=None, saved_state=None):
        self.load()
        conf = json.dumps(self.config.raw)
        s = json.dumps(settings or {})
        st = json.dumps(saved_state or "")
        self.engine.eval(
            "if (source.enable) source.enable(%s, %s, %s);" % (conf, s, st)
        )

    def call(self, method, args=None):
        """Invoke source.<method>(*args) and return the decoded result."""
        self.load()
        args_json = json.dumps(args or [])
        out = self.engine.eval(
            "__bridge_call(%s, %s)" % (json.dumps(method), json.dumps(args_json))
        )
        return json.loads(out) if out else None
