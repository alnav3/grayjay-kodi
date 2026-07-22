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

    def _do_http(self, data):
        method = (data.get("method") or "GET").upper()
        url = data.get("url")
        headers = data.get("headers") or {}
        body = data.get("body")
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
                                         verify=_CA_BUNDLE)
                self._harvest_streams(url, resp.status_code, resp.text)
                return {
                    "url": resp.url, "code": resp.status_code,
                    "headers": dict(resp.headers), "body": resp.text,
                }
            req = _urlreq.Request(url, method=method, headers=headers,
                                  data=body.encode("utf-8") if body else None)
            try:
                r = _urlreq.urlopen(req, timeout=20)
            except _urlreq.HTTPError as he:
                # Non-2xx: return the response rather than raising, so the
                # plugin can inspect status/body (e.g. to detect captchas).
                body_txt = he.read().decode("utf-8", "replace") if hasattr(he, "read") else ""
                return {"url": url, "code": he.code,
                        "headers": dict(he.headers or {}), "body": body_txt}
            with r:
                raw = r.read().decode("utf-8", "replace")
                self._harvest_streams(url, r.status, raw)
                return {"url": r.geturl(), "code": r.status,
                        "headers": dict(r.headers), "body": raw}
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

    def harvest_subtitles(self, details):
        """Extract subtitle tracks from a getContentDetails result.

        Returns a list of dicts with the fields Kodi's DASH manifest needs to
        advertise a `text/vtt` AdaptationSet per track:

            name      — display label (e.g. "English")
            url       — direct VTT URL (preferred), or None for tracks whose
                        content was materialised inline below
            format    — MIME type the plugin reported (typically "text/vtt")
            language  — BCP-47 language code (e.g. "en"); may be None
            _text     — raw VTT body when the plugin had to synthesise the
                        track (auto-generated YouTube ASR, etc.). The caller
                        stages this to disk and points `url` at the served
                        file.

        host_prelude.js materialises `getSubtitles()` synchronously and stashes
        the result as `_subtitles` on the subtitle object before JSON-encoding
        it. Tracks whose `getSubtitles()` returned a Promise (the YouTube ASR
        botguard branch) are left with `_text=None, url=base_url`; we surface
        the URL and let the player fetch directly when supported.

        Tracks that the plugin couldn't materialise are marked `__broken` and
        skipped — otherwise the manifest would advertise a subtitle track
        whose URL returns 0 bytes (YouTube's timedtext endpoint without auth,
        e.g.) and Kodi would show "nothing happens" on selection.

        A few known-auth-gated endpoints (YouTube timedtext, YouTube Music
        captions) also have their URL dropped when we have no materialised
        body — without auth they always return 0 bytes regardless of `kind`.
        """
        out = []
        if not details:
            return out
        for sub in (details.get("subtitles") or []):
            if not isinstance(sub, dict):
                continue
            # Materialisation failed (plugin threw, returned "", or returned a
            # non-string). Trusting the URL in that state only adds a track
            # Kodi will show as silent — drop it.
            if sub.get("__broken"):
                continue
            url = sub.get("url") or None
            text = sub.get("_subtitles") or None
            # If we have neither materialised body nor URL, skip.
            if not text and not url:
                continue
            # YouTube timedtext / captions require auth and return 0 bytes
            # otherwise — when we have no materialised body, the URL is a
            # trap. Other plugins (PeerTube, Rumble, ...) hand out real URLs.
            if not text and _is_gated_captions_url(url):
                continue
            fmt = sub.get("format") or "text/vtt"
            out.append({
                "name": sub.get("name") or (sub.get("language") or "Subtitle"),
                "url": None if text else url,
                "format": fmt,
                "language": sub.get("language") or None,
                "_text": text,
            })
        return out


def _is_gated_captions_url(url):
    """Endpoints whose subtitle body is gated by auth, so the URL is useless
    when our addon isn't logged in. The plugin may hand us a perfectly
    well-formed URL that just returns HTTP 200 / content-length: 0 to us."""
    if not url:
        return False
    u = url.lower()
    if "youtube.com/api/timedtext" in u or "youtube.com/api/captions" in u:
        return True
    if "youtu.be/api/" in u:
        return True
    return False

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
        # Apply engine-specific fixups (e.g. quickjs \- in /u classes) to the
        # SDK and plugin code. Signature was verified above on the original
        # bytes; this only adapts the code for our JS engine.
        combined = "\n;\n".join([
            self.engine.prepare(source_sdk),
            "plugin.config = %s; plugin.settings = %s;" % (
                json.dumps(self.config.raw), json.dumps(self.settings)),
            self.engine.prepare(script),
            "globalThis.source = source; globalThis.plugin = plugin; globalThis.Type = Type;",
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
