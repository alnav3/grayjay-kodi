/*
 * Host-injected packages for the Grayjay plugin runtime (Kodi).
 *
 * Loaded BEFORE Grayjay's own source.js (which defines all model/exception/
 * pager classes, Type, `plugin` and `source`). This file only provides the
 * objects Grayjay normally injects from the native host — http, utility,
 * bridge, logging — plus the __bridge_call entry point. The domParser package
 * is provided separately by dom.js. Everything here routes real I/O to Python
 * __host_* callables registered by bridge.py (JSON-string in / JSON-string out).
 */
(function (global) {
  "use strict";

  function hostCall(name, payload) {
    var res = global[name](JSON.stringify(payload || {}));
    return res ? JSON.parse(res) : null;
  }
  global.__hostCall = hostCall;

  // ---- logging -----------------------------------------------------------
  global.log = function (msg) {
    try { hostCall("__host_log", { msg: typeof msg === "string" ? msg : JSON.stringify(msg) }); } catch (e) {}
  };
  var _noop = function () {};
  global.console = {
    log: global.log, warn: global.log, error: global.log, info: global.log, debug: global.log,
    trace: global.log, dir: global.log, clear: _noop, group: _noop, groupEnd: _noop,
    groupCollapsed: _noop, table: _noop, assert: _noop, count: _noop, countReset: _noop,
    time: _noop, timeEnd: _noop, timeLog: _noop,
  };

  // ---- event loop --------------------------------------------------------
  // quickjs has no event loop. Most Grayjay source methods are synchronous,
  // but YouTube's PO-token (BotGuard) flow is genuinely asynchronous: it defers
  // work with setTimeout and chains Promises, and getContentDetails returns a
  // Promise. So we keep a real timer queue here and let the Python host *drive*
  // it (drain the microtask/job queue, then fire the earliest timer) until the
  // awaited method settles — see jsengine.run_async / __bridge_async_result.
  // Time is virtual: `delay` only orders callbacks; it is never slept on.
  var _timers = [];
  var _timerSeq = 1;
  var _clock = 0;
  function _schedule(fn, delay, args, repeat) {
    if (typeof fn !== "function") return 0;
    var id = _timerSeq++;
    _timers.push({ id: id, fn: fn, time: _clock + (delay || 0),
                   repeat: repeat ? (delay || 0) : null, args: args || [] });
    return id;
  }
  global.setTimeout = function (fn, delay) {
    return _schedule(fn, delay, Array.prototype.slice.call(arguments, 2), false);
  };
  global.setInterval = function (fn, delay) {
    return _schedule(fn, delay, Array.prototype.slice.call(arguments, 2), true);
  };
  global.clearTimeout = function (id) { _timers = _timers.filter(function (t) { return t.id !== id; }); };
  global.clearInterval = global.clearTimeout;
  // Microtask: keep synchronous (matches prior behaviour the scraping paths
  // relied on; the host pump also drains real promise jobs around it).
  global.queueMicrotask = function (fn) { try { fn(); } catch (e) {} };
  // Driver hooks invoked by the Python host pump (jsengine.py):
  global.__pending_timers = function () { return _timers.length; };
  global.__run_one_timer = function () {
    if (_timers.length === 0) return false;
    _timers.sort(function (a, b) { return a.time - b.time; });
    var t = _timers.shift();
    if (t.time > _clock) _clock = t.time;
    if (t.repeat !== null) {
      _timers.push({ id: t.id, fn: t.fn, time: _clock + t.repeat, repeat: t.repeat, args: t.args });
    }
    try { t.fn.apply(null, t.args); } catch (e) { global.log("[timer] " + ((e && e.stack) || e)); }
    return true;
  };

  // ---- encoding polyfills (quickjs lacks these; BotGuard/JSDOM need them) --
  // These are BINARY-safe (each char code is one byte) — unlike utility.*Base64
  // which is UTF-8 — because the BotGuard attestation is raw binary in base64.
  if (typeof global.btoa === "undefined") {
    var _B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    global.btoa = function (input) {
      var str = String(input), out = "";
      for (var block = 0, charCode, i = 0, map = _B64;
           str.charAt(i | 0) || (map = "=", i % 1);
           out += map.charAt(63 & block >> 8 - i % 1 * 8)) {
        charCode = str.charCodeAt(i += 3 / 4);
        if (charCode > 0xFF) throw new Error("btoa: character out of range");
        block = block << 8 | charCode;
      }
      return out;
    };
    global.atob = function (input) {
      var str = String(input).replace(/[=]+$/, ""), out = "";
      if (str.length % 4 === 1) throw new Error("atob: invalid length");
      for (var bc = 0, bs = 0, buffer, i = 0;
           (buffer = str.charAt(i++));
           ~buffer && (bs = bc % 4 ? bs * 64 + buffer : buffer, bc++ % 4)
             ? out += String.fromCharCode(255 & bs >> (-2 * bc & 6)) : 0) {
        buffer = _B64.indexOf(buffer);
      }
      return out;
    };
  }
  if (typeof global.TextEncoder === "undefined") {
    global.TextEncoder = function () {};
    global.TextEncoder.prototype.encode = function (s) {
      var b = unescape(encodeURIComponent(String(s)));   // UTF-8 byte string
      var u = new Uint8Array(b.length);
      for (var i = 0; i < b.length; i++) u[i] = b.charCodeAt(i);
      return u;
    };
  }
  if (typeof global.TextDecoder === "undefined") {
    global.TextDecoder = function () {};
    global.TextDecoder.prototype.decode = function (buf) {
      var bytes = (buf instanceof Uint8Array) ? buf
        : (buf && buf.buffer ? new Uint8Array(buf.buffer) : new Uint8Array(buf || 0));
      var s = "";
      for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
      return decodeURIComponent(escape(s));
    };
  }
  if (typeof global.crypto === "undefined") {
    global.crypto = {
      getRandomValues: function (arr) {
        for (var i = 0; i < arr.length; i++) arr[i] = Math.floor(Math.random() * 256);
        return arr;
      },
    };
  }

  // ---- missing engine globals (quickjs lacks these; JSDOM needs them) -----
  if (typeof global.FinalizationRegistry === "undefined") {
    // GC finalizers: we never run them — harmless for short-lived scrapes.
    global.FinalizationRegistry = function (cb) {
      this.register = function () {}; this.unregister = function () {};
    };
  }
  if (typeof global.WeakRef === "undefined") {
    // Hold a strong reference; deref always returns the value.
    global.WeakRef = function (v) { this._v = v; };
    global.WeakRef.prototype.deref = function () { return this._v; };
  }

  // ---- HTTP package ------------------------------------------------------
  function HttpResponse(o) {
    this.url = o.url; this.code = o.code; this.headers = o.headers || {};
    this.body = o.body; this.isOk = o.code >= 200 && o.code < 300;
  }
  function Http(useAuth) { this._auth = !!useAuth; }
  Http.prototype._req = function (method, url, headers, body) {
    return new HttpResponse(hostCall("__host_http", {
      method: method, url: url, headers: headers || {},
      body: body === undefined ? null : body, useAuth: this._auth,
    }));
  };
  Http.prototype.GET = function (url, headers, useAuth) { return this._req("GET", url, headers, null); };
  Http.prototype.POST = function (url, body, headers, useAuth) { return this._req("POST", url, headers, body); };
  Http.prototype.request = function (method, url, headers, body) { return this._req(method, url, headers, body); };
  Http.prototype.requestWithBody = function (method, url, body, headers) { return this._req(method, url, headers, body); };
  // Batch client (Grayjay PackageHttp.BatchBuilder). Plugins chain
  // .GET/.POST/.DUMMY/.clientGET(...) then .execute() -> array of responses
  // (null for DUMMY slots). The presence of .DUMMY is what the YouTube plugin
  // probes to enable its modern "session client". Run requests sequentially.
  function BatchBuilder(httpClient) { this._http = httpClient; this._reqs = []; }
  BatchBuilder.prototype.request = function (m, u, h, a) { this._reqs.push([m, u, h || {}, null]); return this; };
  BatchBuilder.prototype.requestWithBody = function (m, u, b, h, a) { this._reqs.push([m, u, h || {}, b]); return this; };
  BatchBuilder.prototype.GET = function (u, h, a) { this._reqs.push(["GET", u, h || {}, null]); return this; };
  BatchBuilder.prototype.POST = function (u, b, h, a) { this._reqs.push(["POST", u, h || {}, b]); return this; };
  BatchBuilder.prototype.DUMMY = function () { this._reqs.push(["DUMMY", "", {}, null]); return this; };
  BatchBuilder.prototype.clientRequest = function (cid, m, u, h) { return this.request(m, u, h); };
  BatchBuilder.prototype.clientRequestWithBody = function (cid, m, u, b, h) { return this.requestWithBody(m, u, b, h); };
  BatchBuilder.prototype.clientGET = function (cid, u, h) { return this.GET(u, h); };
  BatchBuilder.prototype.clientPOST = function (cid, u, b, h) { return this.POST(u, b, h); };
  BatchBuilder.prototype.execute = function () {
    var self = this;
    // Preferred: one host call executing the whole batch concurrently (as
    // Grayjay's native client does) — serial round-trips through the single-
    // request bridge are a large slice of YouTube's start-of-playback delay.
    if (typeof global.__host_http_batch !== "undefined") {
      var reqs = this._reqs.map(function (r) {
        return r[0] === "DUMMY" ? null
          : { method: r[0], url: r[1], headers: r[2], body: r[3], useAuth: self._http._auth };
      });
      var out = hostCall("__host_http_batch", { requests: reqs });
      if (out && out.responses) {
        return out.responses.map(function (o) { return o ? new HttpResponse(o) : null; });
      }
    }
    return this._reqs.map(function (r) {
      if (r[0] === "DUMMY") return null;
      return self._http._req(r[0], r[1], r[2], r[3]);
    });
  };
  Http.prototype.batch = function () { return new BatchBuilder(this); };
  global.http = new Http(false);
  global.packageHttp = { newClient: function (useAuth) { return new Http(useAuth); }, getDefaultClient: function (a) { return new Http(a); } };

  // ---- utility package ---------------------------------------------------
  global.utility = {
    toBase64: function (s) { return hostCall("__host_b64encode", { data: s }).out; },
    fromBase64: function (s) { return hostCall("__host_b64decode", { data: s }).out; },
    randomUUID: function () { return hostCall("__host_uuid", {}).out; },
    md5: function (s) { return hostCall("__host_md5", { data: s }).out; },
    md5String: function (s) { return hostCall("__host_md5", { data: s }).out; },
  };

  // ---- bridge package (Grayjay PackageBridge) ----------------------------
  var _UA = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36";
  global.bridge = {
    buildVersion: 290, buildSpecVersion: 3, buildFlavor: "stable", buildPlatform: "android",
    isLoggedIn: function () { return false; },
    captchaUserAgent: _UA, authUserAgent: _UA,
    supportedFeatures: [], supportedContent: [],
    hasPackage: function (name) { return ["Http", "Utilities", "DOMParser", "Bridge"].indexOf(name) >= 0; },
    getHardwareCodecs: function () { return []; },
    // Go straight to the host logger. source.js's own log() delegates to
    // bridge.log, so delegating back to global.log would infinitely recurse.
    log: function (s) { hostCall("__host_log", { msg: typeof s === "string" ? s : JSON.stringify(s) }); },
    toast: function (s) { hostCall("__host_toast", { msg: String(s) }); },
    sleep: function (ms) { hostCall("__host_sleep", { ms: ms }); },
    setTimeout: function (fn, t) { return 0; },   // no event loop in this host
    clearTimeout: function (id) {}, dispose: function (v) {}, devSubmit: function (l, d) {},
  };

  // ---- URL polyfill ------------------------------------------------------
  // Grayjay's V8 provides a global URL; quickjs does not. source.js supplies
  // URLSearchParams but not URL, and plugins use `new URL(...)` heavily. This
  // is a compact, self-contained implementation (no dependency on source.js).
  function _MiniSearchParams(search) {
    this._p = [];
    var q = (search || "").replace(/^\?/, "");
    if (q) q.split("&").forEach(function (pair) {
      if (!pair) return;
      var i = pair.indexOf("=");
      var k = i < 0 ? pair : pair.slice(0, i);
      var v = i < 0 ? "" : pair.slice(i + 1);
      this._p.push([decodeURIComponent(k), decodeURIComponent(v.replace(/\+/g, " "))]);
    }, this);
  }
  _MiniSearchParams.prototype.get = function (k) { for (var i = 0; i < this._p.length; i++) if (this._p[i][0] === k) return this._p[i][1]; return null; };
  _MiniSearchParams.prototype.getAll = function (k) { return this._p.filter(function (e) { return e[0] === k; }).map(function (e) { return e[1]; }); };
  _MiniSearchParams.prototype.has = function (k) { return this.get(k) !== null; };
  _MiniSearchParams.prototype.set = function (k, v) { this.delete(k); this._p.push([k, String(v)]); };
  _MiniSearchParams.prototype.append = function (k, v) { this._p.push([k, String(v)]); };
  _MiniSearchParams.prototype.delete = function (k) { this._p = this._p.filter(function (e) { return e[0] !== k; }); };
  _MiniSearchParams.prototype.forEach = function (cb, t) { this._p.forEach(function (e) { cb.call(t, e[1], e[0], this); }, this); };
  _MiniSearchParams.prototype.toString = function () { return this._p.map(function (e) { return encodeURIComponent(e[0]) + "=" + encodeURIComponent(e[1]); }).join("&"); };

  function _resolve(url, base) {
    if (/^[a-zA-Z][a-zA-Z0-9+.\-]*:/.test(url)) return url;   // already absolute
    if (!base) return url;
    var b = new global.URL(base);
    if (url.indexOf("//") === 0) return b.protocol + url;
    if (url.charAt(0) === "/") return b.protocol + "//" + b.host + url;
    if (url.charAt(0) === "#") return b.protocol + "//" + b.host + b.pathname + b.search + url;
    if (url.charAt(0) === "?") return b.protocol + "//" + b.host + b.pathname + url;
    var path = b.pathname.replace(/[^/]*$/, "");                // strip last segment
    return b.protocol + "//" + b.host + path + url;
  }

  global.URL = function (url, base) {
    if (typeof url !== "string") throw new TypeError("Invalid URL");
    var resolved = _resolve(url, base);
    var m = /^([^:/?#]+:)\/\/(?:([^:@/?#]*)(?::([^@/?#]*))?@)?([^:/?#]*)(?::(\d+))?([^?#]*)(\?[^#]*)?(#.*)?$/.exec(resolved);
    if (!m) {
      // Opaque / non-authority URLs (about:blank, data:, blob:, mailto:, etc.).
      // JSDOM uses about:blank as its default document URL, so we must not throw.
      var op = /^([^:/?#]+:)([^?#]*)(\?[^#]*)?(#.*)?$/.exec(resolved);
      if (op) {
        this.protocol = op[1] || "";
        this.username = ""; this.password = "";
        this.hostname = ""; this.port = ""; this.host = "";
        this.pathname = op[2] || "";
        this.search = op[3] || "";
        this.hash = op[4] || "";
        this.origin = "null";
        this.searchParams = new _MiniSearchParams(this.search);
        this.href = resolved;
        return;
      }
      throw new TypeError("Invalid URL: " + url);
    }
    this.protocol = m[1] || "";
    this.username = m[2] || "";
    this.password = m[3] || "";
    this.hostname = m[4] || "";
    this.port = m[5] || "";
    this.host = this.hostname + (this.port ? ":" + this.port : "");
    this.pathname = m[6] || "/";
    this.search = m[7] || "";
    this.hash = m[8] || "";
    this.origin = this.protocol + "//" + this.host;
    this.searchParams = new _MiniSearchParams(this.search);
    this.href = this.origin + this.pathname + this.search + this.hash;
  };
  global.URL.prototype.toString = function () { return this.href; };

  // ---- subtitle materialisation ----------------------------------------
  // Plugins (notably the YouTube one) return subtitle objects whose text is
  // produced by a JS function `getSubtitles()` — JSON.stringify would drop the
  // function and we'd lose the only way to reach the data. Walk the result
  // after a source method returns; for any subtitle object that exposes
  // `getSubtitles`, call it and stash the resolved text into a serialisable
  // `_subtitles` field so it survives the trip back to Python.
  //
  // YouTube's ASR branch can be async (it awaits a BotGuard POT), so await
  // any Promise the call returns. Returns the input unchanged when there is
  // nothing to do or materialisation fails for one entry — we never let a
  // bad subtitle abort an otherwise-successful source method call.
  function __materialise_subtitles(out) {
    if (!out || !Array.isArray(out.subtitles) || out.subtitles.length === 0) {
      return out;
    }
    for (var i = 0; i < out.subtitles.length; i++) {
      var sub = out.subtitles[i];
      if (!sub || typeof sub !== "object") continue;
      if (typeof sub.getSubtitles !== "function") continue;
      if (typeof sub._subtitles === "string" && sub._subtitles.length > 0) continue;
      try {
        var text = sub.getSubtitles();
        if (text && typeof text.then === "function") {
          // Async branch (YouTube ASR w/o POT). We can't await here — the
          // plugin process pumps the loop in run_async, but this code path
          // is hit during the synchronous __bridge_call return. Leave a
          // marker so the Python side can re-enter the engine and await it
          // explicitly (see bridge.harvest_subtitles).
          sub.__async_subs = true;
          continue;
        }
        if (typeof text === "string" && text.length > 0) {
          sub._subtitles = text;
        }
      } catch (e) {
        global.log("[subtitles] materialise failed: " +
                   ((e && e.stack) || e));
      }
    }
    return out;
  }

  // ---- plugin entry point (called by Python host) ------------------------
  // Flatten pager objects to plain data; pass everything else through.
  function __encode(out) {
    if (out && typeof out.hasMorePagers === "function") {
      return { __pager: true, results: out.results, hasMore: out.hasMore, context: out.context };
    }
    return out === undefined ? null : out;
  }

  // Runs source.<method>(*args). Synchronous results are returned immediately;
  // a returned Promise (e.g. YouTube getContentDetails) is stashed and the host
  // pumps the event loop, then collects it via __bridge_async_result().
  global.__async_slot = null;
  global.__bridge_call = function (method, argsJson) {
    var args = argsJson ? JSON.parse(argsJson) : [];
    var fn = global.source[method];
    if (typeof fn !== "function") {
      throw new Error("source." + method + " is not implemented by this plugin");
    }
    var out = fn.apply(global.source, args);
    if (out && typeof out.then === "function") {
      var slot = { done: false, value: undefined, error: undefined };
      out.then(
        function (v) { slot.value = v; slot.done = true; },
        function (e) { slot.error = (e && e.message) ? e.message : String(e); slot.done = true; }
      );
      global.__async_slot = slot;
      return JSON.stringify({ __async: true });
    }
    __materialise_subtitles(out);
    return JSON.stringify(__encode(out));
  };

  // Polled by the host after each pump step. Reports pending / error / done.
  global.__bridge_async_result = function () {
    var slot = global.__async_slot;
    if (!slot) return JSON.stringify({ __error: "no pending async call" });
    if (!slot.done) return JSON.stringify({ __pending: true });
    global.__async_slot = null;
    if (slot.error !== undefined) return JSON.stringify({ __error: slot.error });
    return JSON.stringify({ __done: true, result: __encode(slot.value) });
  };
})(this);
