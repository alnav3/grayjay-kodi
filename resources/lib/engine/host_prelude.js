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
  global.console = { log: global.log, warn: global.log, error: global.log, info: global.log, debug: global.log };

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
  // Batch client: plugins call http.batch().GET(...).execute(). Run sequentially.
  Http.prototype.batch = function () {
    var self = this, queue = [];
    return {
      GET: function (u, h, a) { queue.push(["GET", u, h, null]); return this; },
      POST: function (u, b, h, a) { queue.push(["POST", u, h, b]); return this; },
      execute: function () { return queue.map(function (q) { return self._req(q[0], q[1], q[2], q[3]); }); },
    };
  };
  global.http = new Http(false);
  global.packageHttp = { newClient: function (useAuth) { return new Http(useAuth); }, getDefaultClient: function (a) { return new Http(a); } };

  // ---- utility package ---------------------------------------------------
  global.utility = {
    toBase64: function (s) { return hostCall("__host_b64encode", { data: s }).out; },
    fromBase64: function (s) { return hostCall("__host_b64decode", { data: s }).out; },
    randomUUID: function () { return hostCall("__host_uuid", {}).out; },
    md5: function (s) { return hostCall("__host_md5", { data: s }).out; },
  };

  // ---- bridge package (Grayjay PackageBridge) ----------------------------
  var _UA = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36";
  global.bridge = {
    buildVersion: 290, buildSpecVersion: 3, buildFlavor: "stable", buildPlatform: "android",
    isLoggedIn: false, captchaUserAgent: _UA, authUserAgent: _UA,
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
    if (!m) throw new TypeError("Invalid URL: " + url);
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

  // ---- plugin entry point (called by Python host) ------------------------
  // Runs source.<method>(*args); flattens pager objects to plain data.
  global.__bridge_call = function (method, argsJson) {
    var args = argsJson ? JSON.parse(argsJson) : [];
    var fn = global.source[method];
    if (typeof fn !== "function") {
      throw new Error("source." + method + " is not implemented by this plugin");
    }
    var out = fn.apply(global.source, args);
    if (out && typeof out.hasMorePagers === "function") {
      return JSON.stringify({ __pager: true, results: out.results, hasMore: out.hasMore, context: out.context });
    }
    return JSON.stringify(out === undefined ? null : out);
  };
})(this);
