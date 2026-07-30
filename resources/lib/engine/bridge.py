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

from ..kodiutils import log, resolve_ca_bundle
from .jsengine import JSEngine

try:
    import requests as _requests
except ImportError:
    _requests = None

import urllib.request as _urlreq
import urllib.error  # noqa: F401  (exposes _urlreq.HTTPError reliably)


# Resolved once: a filesystem path to a CA bundle, or True (requests default).
_CA_BUNDLE = resolve_ca_bundle()


_DIR = os.path.dirname(os.path.abspath(__file__))
HOST_PRELUDE_JS = os.path.join(_DIR, "host_prelude.js")  # host-injected packages
SOURCE_JS = os.path.join(_DIR, "source.js")              # Grayjay's own SDK prelude
DOM_JS = os.path.join(_DIR, "dom.js")                    # domParser package
UMP_SHIM_JS = os.path.join(_DIR, "ump_shim.js")          # YouTube UMP/SABR fallback shim

# Hosts whose requests are small, fast JSON round-trips (not large/streamed
# content) where an automatic retry costs almost nothing -- safe to retry
# blindly. YouTube's own endpoints (player.js, googlevideo segments, etc.)
# are deliberately excluded: those can legitimately take tens of seconds
# (see _read_capped), and retrying one from scratch on a slow home-WiFi
# blip would multiply, not fix, the delay a user feels.
_RETRYABLE_HOSTS = ("solver.grayjay.app", "solutions.grayjay.app")


class SignatureError(Exception):
    pass


class PluginBridge(object):
    def __init__(self, config):
        self.config = config           # sources.config.SourceConfig
        self.engine = JSEngine()
        self._loaded = False
        self.settings = {}             # per-source plugin settings (by variable)
        self._stream_harvest = []      # adaptive formats sniffed from responses
        self._muxed_harvest = []       # muxed (progressive) formats sniffed
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
        return json.dumps(self._do_http(json.loads(payload_json)))

    def _host_http_batch(self, payload_json):
        """Execute a BatchBuilder's requests concurrently.

        Grayjay runs `http.batch()` requests in parallel; running them one
        after another through the single-request bridge multiplies every
        network round-trip — the YouTube session-client init batches several
        innertube calls, and serial execution is a large slice of the
        select-to-playback delay. DUMMY slots come in as null and stay null;
        response order matches request order."""
        reqs = json.loads(payload_json).get("requests") or []
        results = [None] * len(reqs)
        live = [(i, r) for i, r in enumerate(reqs) if r]
        if len(live) == 1:
            i, r = live[0]
            results[i] = self._do_http(r)
        elif live:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(6, len(live))) as pool:
                futures = [(i, pool.submit(self._do_http, r)) for i, r in live]
                for i, fut in futures:
                    results[i] = fut.result()
        return json.dumps({"responses": results})

    def _decode_response_body(self, raw_bytes):
        """Text responses (JSON/HTML/etc.) must stay plain strings -- lots of
        existing plugin code does `JSON.parse(resp.body)` directly. Binary
        responses (e.g. UMP/SABR segment bytes) aren't valid UTF-8, so a
        strict decode attempt fails and we base64-encode instead; the JS side
        already treats a string `resp.body` it can't JSON.parse as base64
        (`Uint8Array.from(atob(data), ...)`, script.js:4501) -- this is the
        same contract Grayjay's native host uses for binary bodies."""
        try:
            return raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return base64.b64encode(raw_bytes).decode("ascii")

    @staticmethod
    def _read_capped(fileobj, max_seconds=45.0, chunk_size=65536):
        """r.read() with no size argument blocks until the socket hits true
        EOF -- fine for an ordinary bounded response, but some googlevideo
        endpoints (`keepalive=yes` in the query string -- part of YouTube's
        UMP/SABR streaming protocol) hold the connection open and keep
        trickling bytes indefinitely, resetting urllib's per-read timeout
        each time without ever sending EOF. Read in chunks and stop once
        max_seconds of wall-clock time have passed regardless of whether the
        connection is still open, treating whatever arrived as the full
        response. 45s was picked empirically: the UMP/SABR combined-source
        endpoint's real (bounded) response can legitimately take 30-40s to
        arrive server-side (looks like BotGuard/attestation verification
        delay, not a stall) -- a 20s cap cut it off just short of success in
        testing, so this only ever cuts off responses that genuinely never
        finish, not slow-but-real ones."""
        import time as _time
        start = _time.monotonic()
        chunks = []
        while _time.monotonic() - start < max_seconds:
            chunk = fileobj.read(chunk_size)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    def _do_http(self, data):
        """Transient network blips (a dropped connection, a 5xx/429) hitting
        the small, fast solver-cache endpoints are common on this box's WiFi,
        but plugin code often treats a single failed request as fatal for the
        whole operation (e.g. the YouTube source's N-parameter solver lookup
        has no retry/catch, unlike its signature-solver counterpart -- one bad
        response aborts cipher prep entirely). Retry a couple of times with a
        short backoff before handing a failure back to JS, so we only ever
        surface a real, persistent failure. Scoped to _RETRYABLE_HOSTS only --
        see its docstring for why large/streamed content fetches must not be
        retried the same way."""
        url = data.get("url") or ""
        if not any(host in url for host in _RETRYABLE_HOSTS):
            return self._do_http_once(data)
        import time as _time
        last = None
        for attempt in range(3):
            last = self._do_http_once(data)
            code = last.get("code") or 0
            retryable = code == 0 or code == 429 or code >= 500
            if not retryable or attempt == 2:
                return last
            _time.sleep(0.4 * (attempt + 1))
        return last

    def _do_http_once(self, data):
        method = (data.get("method") or "GET").upper()
        url = data.get("url")
        headers = data.get("headers") or {}
        body = data.get("body")
        if body and data.get("bodyIsBase64"):
            body = base64.b64decode(body)
        # allowUrls enforcement (basic): if config restricts domains, honor it.
        if not self.config.url_allowed(url):
            return {"url": url, "code": 0, "headers": {}, "body": "",
                    "error": "URL blocked by plugin allowUrls"}
        # Ensure a browser-like UA unless the plugin set one (many sites 403 the
        # default urllib/python agent).
        if not any(k.lower() == "user-agent" for k in headers):
            headers["User-Agent"] = self._default_ua()
        # Default a JSON content-type for bodied POST/PUT when the plugin didn't
        # set one. Grayjay's native http client does this; without it YouTube's
        # WEB innertube /player rejects the request (400 FAILED_PRECONDITION).
        if body and method in ("POST", "PUT", "PATCH") \
                and not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = "application/json"
        try:
            if _requests is not None:
                resp = _requests.request(method, url, headers=headers,
                                         data=body, timeout=20, allow_redirects=True,
                                         verify=_CA_BUNDLE, stream=True)
                # See _read_capped: some googlevideo endpoints (keepalive=yes)
                # hold the connection open indefinitely -- resp.content would
                # block forever waiting for it to close.
                import time as _time
                start = _time.monotonic()
                chunks = []
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        chunks.append(chunk)
                    if _time.monotonic() - start >= 45.0:
                        break
                raw_bytes = b"".join(chunks)
                body_out = self._decode_response_body(raw_bytes)
                self._harvest_streams(url, resp.status_code,
                                      raw_bytes.decode("utf-8", "replace"))
                return {
                    "url": resp.url, "code": resp.status_code,
                    "headers": dict(resp.headers), "body": body_out,
                }
            req = _urlreq.Request(url, method=method, headers=headers,
                                  data=body if isinstance(body, bytes)
                                  else (body.encode("utf-8") if body else None))
            try:
                r = _urlreq.urlopen(req, timeout=20)
            except _urlreq.HTTPError as he:
                # Non-2xx: return the response rather than raising, so the
                # plugin can inspect status/body (e.g. to detect captchas).
                raw = self._read_capped(he) if hasattr(he, "read") else b""
                body_txt = self._decode_response_body(raw)
                return {"url": url, "code": he.code,
                        "headers": dict(he.headers or {}), "body": body_txt}
            with r:
                raw_bytes = self._read_capped(r)
                body_out = self._decode_response_body(raw_bytes)
                self._harvest_streams(url, r.status,
                                      raw_bytes.decode("utf-8", "replace"))
                return {"url": r.geturl(), "code": r.status,
                        "headers": dict(r.headers), "body": body_out}
        except Exception as exc:
            log("http error %s: %s" % (url, exc), "warning")
            return {"url": url, "code": 0, "headers": {}, "body": "", "error": str(exc)}

    def _harvest_streams(self, url, code, body):
        """Sniff direct-URL adaptive formats from a YouTube player response.

        The plugin returns adaptive sources to us with deciphered *video* URLs
        but no audio URLs (audio is meant to be muxed JS-side via SABR). The raw
        ANDROID_VR `youtubei/v1/player` response, however, carries direct,
        range-able URLs for *both* video and audio — exactly what we need to
        synthesise a DASH manifest for inputstream.adaptive. Capture the last
        such set so the router can build an MPD for playback. Best-effort and
        YouTube-shaped; harmless (and inert) for other sources."""
        if code != 200 or "youtubei/v1/player" not in (url or ""):
            return
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            return
        sd = data.get("streamingData") or {}
        fmts = sd.get("adaptiveFormats") or []
        if any(f.get("url") for f in fmts):
            # Same response's muxed (progressive) formats — kept together so the
            # muxed URLs come from the client that returns direct URLs
            # (ANDROID_VR), not the SABR-only WEB response.
            self._stream_harvest = fmts
            self._muxed_harvest = [f for f in (sd.get("formats") or [])
                                   if f.get("url")]

    def harvested_streams(self):
        """Adaptive formats (with direct URLs) seen on the last player call."""
        return self._stream_harvest

    def harvested_muxed(self):
        """Muxed/progressive formats (with direct URLs) — single playable URLs."""
        return self._muxed_harvest

    @staticmethod
    def _default_ua():
        # Desktop Chrome. Plugins that need a mobile/iOS/Android UA set it
        # explicitly per request; the requests that omit a UA (e.g. YouTube's
        # WEB innertube /player call) expect a *desktop browser* UA — a mobile
        # default makes the WEB client context mismatch and YouTube returns
        # 400 FAILED_PRECONDITION. Match the plugin's own USER_AGENT_WINDOWS.
        return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36")

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
        e.register("__host_http_batch", self._host_http_batch)
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
        with open(UMP_SHIM_JS, "r", encoding="utf-8") as fh:
            ump_shim = fh.read()
        # Apply engine-specific fixups (e.g. quickjs \- in /u classes) to the
        # SDK and plugin code. Signature was verified above on the original
        # bytes; this only adapts the code for our JS engine.
        combined = "\n;\n".join([
            self.engine.prepare(source_sdk),
            "plugin.config = %s; plugin.settings = %s;" % (
                json.dumps(self.config.raw), json.dumps(self.settings)),
            self.engine.prepare(script),
            "globalThis.source = source; globalThis.plugin = plugin; globalThis.Type = Type;",
            ump_shim,
        ])
        self.engine.eval(combined)
        self._loaded = True

    def enable(self, settings=None, saved_state=None):
        # Stash settings before load() so they're injected as plugin.settings
        # in the same eval as the SDK + plugin.
        if settings is not None:
            self.settings = settings
        self.load()
        conf = json.dumps(self.config.raw)
        s = json.dumps(self.settings or {})
        st = json.dumps(saved_state or "")
        self.engine.eval(
            "if (source.enable) source.enable(%s, %s, %s);" % (conf, s, st)
        )
        # source.enable may kick off async init (e.g. YouTube session client);
        # let any queued promise jobs run so state is settled before first call.
        try:
            self.engine.drain_jobs()
        except Exception:
            pass

    def save_state(self):
        """Capture `source.saveState()` (a string), or None when the plugin
        doesn't implement it / has nothing to save. Persisted by the caller and
        fed back into the next `enable(config, settings, savedState)` so
        expensive per-session init (e.g. YouTube's session client) survives the
        short-lived Kodi plugin process."""
        if not self._loaded:
            return None
        try:
            out = self.engine.eval(
                "JSON.stringify((function(){"
                "  if (typeof source.saveState !== 'function') return '';"
                "  var s = source.saveState();"
                "  return typeof s === 'string' ? s : (s ? JSON.stringify(s) : '');"
                "})())"
            )
            state = json.loads(out) if out else ""
        except Exception as exc:
            log("saveState failed for %s: %s" % (self.config.id, exc), "debug")
            return None
        return state if isinstance(state, str) and state else None

    def _async_deadline(self):
        """How long to pump the event loop for an async source method (s)."""
        try:
            from ..kodiutils import get_setting
            return float(get_setting("async_timeout", "90"))
        except Exception:
            return 90.0

    def call(self, method, args=None):
        """Invoke source.<method>(*args) and return the decoded result.

        Most methods are synchronous, but some (notably YouTube's
        getContentDetails, which drives the async BotGuard PO-token flow) return
        a Promise; __bridge_call signals that with {__async:true} and we pump the
        event loop until it settles.
        """
        self.load()
        args_json = json.dumps(args or [])
        out = self.engine.eval(
            "__bridge_call(%s, %s)" % (json.dumps(method), json.dumps(args_json))
        )
        data = json.loads(out) if out else None
        if isinstance(data, dict) and data.get("__async"):
            return self.engine.run_async(deadline_s=self._async_deadline())
        return data

    def close(self):
        """Tear down the underlying JS engine (kills the qjs subprocess, if
        any). Only needed by long-lived holders of a bridge (e.g. the
        service's UMP session registry) -- ephemeral per-request bridges
        just get garbage collected with the process."""
        self.engine.close()


def create_enabled_bridge(source_id):
    """Look up a source's config and return a ready-to-call PluginBridge, or
    None if the source doesn't exist. Feeds back the plugin's persisted
    saveState() so a fresh process doesn't redo expensive session init
    (YouTube: innertube context + BotGuard) on every invocation, and persists
    it again afterwards. Shared by router.py's per-request bridge cache and
    the background service's UMP session registry."""
    from ..sources import manager, plugin_settings, plugin_state
    cfg = manager.get_source(source_id)
    if cfg is None:
        return None
    bridge = PluginBridge(cfg)
    bridge.enable(settings=plugin_settings.load(cfg),
                  saved_state=plugin_state.load(cfg) or None)
    try:
        state = bridge.save_state()
        if state:
            plugin_state.save(cfg, state)
    except Exception as exc:
        log("persisting state failed: %s" % exc, "debug")
    return bridge
