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


PACKAGES_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "packages.js")


class PluginBridge(object):
    def __init__(self, config):
        self.config = config           # sources.config.SourceConfig
        self.engine = JSEngine()
        self._loaded = False

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
        try:
            if _requests is not None:
                resp = _requests.request(method, url, headers=headers,
                                         data=body, timeout=20)
                return json.dumps({
                    "url": url, "code": resp.status_code,
                    "headers": dict(resp.headers), "body": resp.text,
                })
            req = _urlreq.Request(url, method=method, headers=headers,
                                  data=body.encode("utf-8") if body else None)
            with _urlreq.urlopen(req, timeout=20) as r:
                raw = r.read().decode("utf-8", "replace")
                return json.dumps({
                    "url": url, "code": r.status,
                    "headers": dict(r.headers), "body": raw,
                })
        except Exception as exc:
            log("http error %s: %s" % (url, exc), "warning")
            return json.dumps({"url": url, "code": 0, "headers": {}, "body": "", "error": str(exc)})

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

    def _register_host(self):
        e = self.engine
        e.register("__host_log", self._host_log)
        e.register("__host_http", self._host_http)
        e.register("__host_b64encode", self._host_b64encode)
        e.register("__host_b64decode", self._host_b64decode)
        e.register("__host_uuid", self._host_uuid)
        e.register("__host_md5", self._host_md5)

    # -- lifecycle --------------------------------------------------------
    def load(self):
        if self._loaded:
            return
        self._register_host()
        with open(PACKAGES_JS, "r", encoding="utf-8") as fh:
            self.engine.eval(fh.read())
        # Expose the parsed config to the plugin as `plugin.config`.
        self.engine.eval("var plugin = %s;" % json.dumps({
            "config": self.config.raw,
        }))
        with open(self.config.script_path, "r", encoding="utf-8") as fh:
            self.engine.eval(fh.read())
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
