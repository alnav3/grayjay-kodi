# -*- coding: utf-8 -*-
"""DOMParser backend for the Grayjay `domParser` package.

Grayjay's PackageDOMParser is backed by jsoup. We reimplement the same surface
(parseFromString -> DOMNode with querySelector/getElementsBy*/attributes/...)
using BeautifulSoup + soupsieve (vendored, pure-Python) so CSS selectors behave
close to jsoup. The heavy work stays in Python; the JS side (dom.js) only holds
integer node handles and proxies each property/method through __host_dom_op.
"""
import json

try:
    from bs4 import BeautifulSoup
    from bs4.element import Tag
except Exception:  # pragma: no cover - vendor path added by jsengine import
    BeautifulSoup = None
    Tag = None


def _collapse(text):
    return " ".join((text or "").split())


class DOMRegistry(object):
    def __init__(self):
        self._nodes = {}      # handle -> node
        self._by_id = {}      # id(node) -> handle (dedupe)
        self._next = 1

    # -- handle management -----------------------------------------------
    def _handle_for(self, node):
        if node is None:
            return None
        key = id(node)
        h = self._by_id.get(key)
        if h is None:
            h = self._next
            self._next += 1
            self._nodes[h] = node
            self._by_id[key] = h
        return h

    def _node(self, handle):
        return self._nodes.get(int(handle))

    # -- host entry points ------------------------------------------------
    def parse(self, payload_json):
        data = json.loads(payload_json)
        soup = BeautifulSoup(data.get("html", ""), "html.parser")
        return json.dumps({"handle": self._handle_for(soup)})

    def op(self, payload_json):
        data = json.loads(payload_json)
        node = self._node(data.get("h"))
        name = data.get("op")
        arg = data.get("arg")
        if node is None:
            return json.dumps({"kind": "value", "value": None})
        return json.dumps(self._dispatch(node, name, arg))

    # -- op implementations ----------------------------------------------
    def _val(self, v):
        return {"kind": "value", "value": v}

    def _node_ret(self, n):
        return {"kind": "node", "handle": self._handle_for(n)}

    def _nodes_ret(self, ns):
        return {"kind": "nodes", "handles": [self._handle_for(n) for n in ns if n is not None]}

    def _element_children(self, node):
        return [c for c in getattr(node, "children", []) if _is_tag(c)]

    def _dispatch(self, node, name, arg):
        # --- value properties ---
        if name == "nodeType":
            return self._val(_tagname(node))
        if name == "tagName":
            return self._val(_tagname(node).upper())
        if name == "textContent":
            return self._val(_collapse(node.get_text(" ")))
        if name == "text":
            t = _collapse(node.get_text(" "))
            return self._val(t if t else self._data(node))
        if name == "data":
            return self._val(self._data(node))
        if name == "innerHTML":
            return self._val(node.decode_contents() if _is_tag(node) else "")
        if name == "outerHTML":
            return self._val(str(node))
        if name == "attributes":
            return {"kind": "map", "value": _attrs(node)}
        if name == "classList":
            return self._val(_classlist(node))
        if name == "className":
            return self._val(" ".join(_classlist(node)))
        if name == "getAttribute":
            return self._val(_attrs(node).get(arg, ""))
        # --- node-returning ---
        if name == "firstChild":
            kids = self._element_children(node)
            return self._node_ret(kids[0] if kids else None)
        if name == "lastChild":
            kids = self._element_children(node)
            return self._node_ret(kids[-1] if kids else None)
        if name in ("parentNode", "parentElement"):
            p = node.parent
            return self._node_ret(p if _is_tag(p) else None)
        if name == "getElementById":
            return self._node_ret(node.find(id=arg) if _is_tag(node) else None)
        if name == "querySelector":
            return self._node_ret(node.select_one(arg) if _is_tag(node) else None)
        # --- list-returning ---
        if name == "childNodes":
            return self._nodes_ret(self._element_children(node))
        if name == "getElementsByClassName":
            return self._nodes_ret(node.find_all(class_=arg) if _is_tag(node) else [])
        if name == "getElementsByTagName":
            return self._nodes_ret(node.find_all(arg) if _is_tag(node) else [])
        if name == "getElementsByName":
            return self._nodes_ret(node.find_all(attrs={"name": arg}) if _is_tag(node) else [])
        if name == "querySelectorAll":
            return self._nodes_ret(node.select(arg) if _is_tag(node) else [])
        # --- serialization ---
        if name == "toNodeTreeJson":
            return self._val(json.dumps(self._tree(node)))
        return self._val(None)

    def _data(self, node):
        # jsoup .data() returns raw contents of script/style/comment nodes.
        if _is_tag(node) and node.name in ("script", "style"):
            return node.string or "".join(node.strings)
        return ""

    def _tree(self, node):
        return {
            "name": _tagname(node),
            "value": _collapse(node.get_text(" ")),
            "attributes": _attrs(node),
            "children": [self._tree(c) for c in self._element_children(node)],
        }


def _is_tag(n):
    return Tag is not None and isinstance(n, Tag)


def _tagname(node):
    name = getattr(node, "name", None)
    return name if name else ""


def _attrs(node):
    out = {}
    for k, v in getattr(node, "attrs", {}).items():
        out[k] = " ".join(v) if isinstance(v, list) else str(v)
    return out


def _classlist(node):
    c = getattr(node, "attrs", {}).get("class", [])
    if isinstance(c, str):
        return c.split()
    return list(c)
