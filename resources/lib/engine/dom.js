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
})(this);
