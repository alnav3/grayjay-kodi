/*
 * domParser package (Grayjay PackageDOMParser equivalent).
 *
 * Mirrors the jsoup-backed API: domParser.parseFromString(html) -> DOMNode,
 * with DOMNode exposing the same properties (nodeType, tagName, textContent,
 * attributes, classList, ...) and methods (querySelector, getElementsByTagName,
 * getAttribute, ...). Each access proxies to the Python DOM registry via the
 * __host_dom_* callables; DOMNode only carries an integer handle.
 */
(function (global) {
  "use strict";

  function domHost(name, payload) {
    var res = global[name](JSON.stringify(payload || {}));
    return res ? JSON.parse(res) : null;
  }

  function wrap(ret) {
    if (!ret) return null;
    if (ret.kind === "value") return ret.value;
    if (ret.kind === "map") return ret.value;
    if (ret.kind === "node") return ret.handle == null ? null : new DOMNode(ret.handle);
    if (ret.kind === "nodes") return ret.handles.map(function (h) { return new DOMNode(h); });
    return null;
  }

  function op(handle, name, arg) {
    return wrap(domHost("__host_dom_op", { h: handle, op: name, arg: arg === undefined ? null : arg }));
  }

  function DOMNode(handle) {
    this._h = handle;
  }

  // Property-style members (accessed without parentheses), defined as getters
  // so `node.textContent` triggers a host call, matching @V8Property.
  var PROPS = ["nodeType", "tagName", "textContent", "text", "data",
               "innerHTML", "outerHTML", "attributes", "classList", "className",
               "firstChild", "lastChild", "parentNode", "parentElement",
               "childNodes"];
  PROPS.forEach(function (p) {
    Object.defineProperty(DOMNode.prototype, p, {
      get: function () { return op(this._h, p); },
      enumerable: true,
    });
  });

  // Method-style members (@V8Function).
  DOMNode.prototype.getAttribute = function (k) { return op(this._h, "getAttribute", k); };
  DOMNode.prototype.getElementById = function (id) { return op(this._h, "getElementById", id); };
  DOMNode.prototype.getElementsByClassName = function (c) { return op(this._h, "getElementsByClassName", c); };
  DOMNode.prototype.getElementsByTagName = function (t) { return op(this._h, "getElementsByTagName", t); };
  DOMNode.prototype.getElementsByName = function (n) { return op(this._h, "getElementsByName", n); };
  DOMNode.prototype.querySelector = function (q) { return op(this._h, "querySelector", q); };
  DOMNode.prototype.querySelectorAll = function (q) { return op(this._h, "querySelectorAll", q); };
  DOMNode.prototype.toNodeTreeJson = function () { return op(this._h, "toNodeTreeJson"); };
  DOMNode.prototype.toNodeTree = function () { return JSON.parse(this.toNodeTreeJson()); };
  DOMNode.prototype.dispose = function () { /* handles are freed with the engine */ };

  global.DOMNode = DOMNode;
  global.domParser = {
    parseFromString: function (html) {
      var r = domHost("__host_dom_parse", { html: html });
      return new DOMNode(r.handle);
    },
  };
  // Some plugins reference a global DOMParser constructor (web-style).
  global.DOMParser = function () {};
  global.DOMParser.prototype.parseFromString = function (html) {
    return global.domParser.parseFromString(html);
  };

  // Minimal JSDOM shim. The YouTube plugin's BotGuard flow calls
  // `new JSDOM()` and then reads `currentJSDOM.window` and
  // `currentJSDOM.window.document`. We provide a window/document with the
  // properties the obfuscated VM and cipher player JS touch (navigator,
  // location, createElement, body, head) as harmless stubs — BotGuard runs
  // its own server-side attestation through `http.POST`, it doesn't
  // *exercise* the DOM, it just needs the globals to exist with the right
  // shape.
  function JSDOMElement(tag) {
    this.tagName = (tag || "").toUpperCase();
    this.style = {};
    this.children = [];
    this.childNodes = [];
    this.parentNode = null;
    this.firstChild = null;
    this.lastChild = null;
    this.nextSibling = null;
    this.previousSibling = null;
    this.nodeType = 1;
    this.nodeName = this.tagName;
    this.attributes = {};
    this._innerHTML = "";
    this._textContent = "";
  }
  JSDOMElement.prototype.appendChild = function (c) {
    c.parentNode = this;
    this.children.push(c);
    this.childNodes.push(c);
    this.firstChild = this.children[0] || null;
    this.lastChild = this.children[this.children.length - 1] || null;
    return c;
  };
  JSDOMElement.prototype.removeChild = function (c) {
    var i = this.children.indexOf(c);
    if (i >= 0) this.children.splice(i, 1);
    var j = this.childNodes.indexOf(c);
    if (j >= 0) this.childNodes.splice(j, 1);
    c.parentNode = null;
    return c;
  };
  JSDOMElement.prototype.setAttribute = function (k, v) { this.attributes[k] = v; };
  JSDOMElement.prototype.getAttribute = function (k) { return this.attributes[k] || null; };
  JSDOMElement.prototype.hasAttribute = function (k) { return k in this.attributes; };
  JSDOMElement.prototype.removeAttribute = function (k) { delete this.attributes[k]; };
  JSDOMElement.prototype.cloneNode = function () {
    var n = new JSDOMElement(this.tagName);
    n.attributes = JSON.parse(JSON.stringify(this.attributes));
    return n;
  };
  JSDOMElement.prototype.addEventListener = function () {};
  JSDOMElement.prototype.removeEventListener = function () {};
  JSDOMElement.prototype.dispatchEvent = function () { return true; };
  Object.defineProperty(JSDOMElement.prototype, "innerHTML", {
    get: function () { return this._innerHTML; },
    set: function (v) { this._innerHTML = v; },
  });
  Object.defineProperty(JSDOMElement.prototype, "textContent", {
    get: function () { return this._textContent; },
    set: function (v) { this._textContent = v; },
  });

  function JSDOMDocument() {
    this.documentElement = new JSDOMElement("html");
    this.head = new JSDOMElement("head");
    this.body = new JSDOMElement("body");
    this.documentElement.appendChild(this.head);
    this.documentElement.appendChild(this.body);
  }
  JSDOMDocument.prototype.createElement = function (tag) {
    return new JSDOMElement(tag || "div");
  };
  JSDOMDocument.prototype.createElementNS = function (ns, tag) {
    return this.createElement(tag);
  };
  JSDOMDocument.prototype.createTextNode = function (text) {
    var n = new JSDOMElement("#text");
    n.nodeType = 3;
    n._textContent = text || "";
    return n;
  };
  JSDOMDocument.prototype.getElementById = function (id) {
    return null;
  };
  JSDOMDocument.prototype.querySelector = function () { return null; };
  JSDOMDocument.prototype.querySelectorAll = function () { return []; };
  JSDOMDocument.prototype.getElementsByTagName = function () { return []; };
  JSDOMDocument.prototype.addEventListener = function () {};
  JSDOMDocument.prototype.removeEventListener = function () {};

  function JSDOMNavigator() {
    this.userAgent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    + "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";
    this.platform = "Linux x86_64";
    this.language = "en-US";
    this.languages = ["en-US", "en"];
    this.vendor = "";
    this.vendorSub = "";
    this.product = "Gecko";
    this.productSub = "20030107";
    this.hardwareConcurrency = 4;
  }

  function JSDOMLocation() {
    this.protocol = "https:";
    this.host = "www.youtube.com";
    this.hostname = "www.youtube.com";
    this.port = "";
    this.pathname = "/";
    this.search = "";
    this.hash = "";
    this.href = "https://www.youtube.com/";
    this.origin = "https://www.youtube.com";
  }

  function JSDOMWindow() {
    var self = this;
    this.navigator = new JSDOMNavigator();
    this.location = new JSDOMLocation();
    this.document = new JSDOMDocument();
    this.window = self;        // window.window === window
    this.self = self;
    this.parent = self;
    this.top = self;
    this.frames = self;
    this.globalThis = self;
    this.length = 0;
    this.closed = false;
    this.frames = {};
    this.opener = null;
    this.parent = self;
    this.status = "";
    this.name = "";
  }
  JSDOMWindow.prototype.setTimeout = function (fn, t) {
    return global.setTimeout ? global.setTimeout(fn, t) : 0;
  };
  JSDOMWindow.prototype.setInterval = function (fn, t) {
    return global.setInterval ? global.setInterval(fn, t) : 0;
  };
  JSDOMWindow.prototype.clearTimeout = function (id) {
    if (global.clearTimeout) global.clearTimeout(id);
  };
  JSDOMWindow.prototype.clearInterval = function (id) {
    if (global.clearInterval) global.clearInterval(id);
  };
  JSDOMWindow.prototype.addEventListener = function () {};
  JSDOMWindow.prototype.removeEventListener = function () {};
  JSDOMWindow.prototype.postMessage = function () {};
  JSDOMWindow.prototype.close = function () { this.closed = true; };
  JSDOMWindow.prototype.alert = function () {};
  JSDOMWindow.prototype.confirm = function () { return false; };
  JSDOMWindow.prototype.prompt = function () { return null; };
  JSDOMWindow.prototype.focus = function () {};
  JSDOMWindow.prototype.blur = function () {};
  JSDOMWindow.prototype.scroll = function () {};
  JSDOMWindow.prototype.scrollBy = function () {};
  JSDOMWindow.prototype.scrollTo = function () {};
  JSDOMWindow.prototype.fetch = function () { return Promise.reject(new Error("no fetch")); };
  JSDOMWindow.prototype.getComputedStyle = function () { return {}; };
  JSDOMWindow.prototype.btoa = function (s) {
    return global.btoa ? global.btoa(s) : "";
  };
  JSDOMWindow.prototype.atob = function (s) {
    return global.atob ? global.atob(s) : "";
  };
  JSDOMWindow.prototype.requestAnimationFrame = function (cb) {
    return global.setTimeout ? global.setTimeout(cb, 16) : 0;
  };
  JSDOMWindow.prototype.cancelAnimationFrame = function (id) {
    if (global.clearTimeout) global.clearTimeout(id);
  };

  function JSDOM() {
    this.window = new JSDOMWindow();
  }
  global.JSDOM = JSDOM;
})(this);
