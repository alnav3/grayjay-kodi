# -*- coding: utf-8 -*-
"""Subscription groups (Grayjay-style).

Grayjay lets you bundle subscribed channels into named groups, so the
Subscriptions screen offers "All" plus one feed per group. Groups are stored
locally, independent of the subscriptions themselves; a member is just a
reference to a subscription by its (source, url) key.

Stored as JSON at <profile>/sub_groups.json:
    [{"id": "<slug>", "name": "Music",
      "members": [{"source": "<id>", "url": "<channel url>"}, ...]}, ...]

Removing a subscription does not rewrite groups; a member that no longer maps
to a live subscription is simply skipped when a group feed is built.
"""
import json
import os
import re

from ..kodiutils import profile_path, log


def _path():
    return os.path.join(profile_path(), "sub_groups.json")


def list_groups():
    p = _path()
    if not os.path.isfile(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        log("failed to read groups: %s" % exc, "warning")
        return []


def _save(groups):
    with open(_path(), "w", encoding="utf-8") as fh:
        json.dump(groups, fh, indent=2)


def get_group(group_id):
    for g in list_groups():
        if g.get("id") == group_id:
            return g
    return None


def _slugify(name, existing):
    base = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (name or "").lower())).strip("-")
    base = base or "group"
    slug, n = base, 2
    while slug in existing:
        slug = "%s-%d" % (base, n)
        n += 1
    return slug


def create_group(name):
    """Create a group; returns it. Names need not be unique (ids are)."""
    groups = list_groups()
    gid = _slugify(name, {g.get("id") for g in groups})
    group = {"id": gid, "name": name or gid, "members": []}
    groups.append(group)
    _save(groups)
    log("created group %s (%s)" % (group["name"], gid), "info")
    return group


def rename_group(group_id, name):
    groups = list_groups()
    for g in groups:
        if g.get("id") == group_id:
            g["name"] = name
            _save(groups)
            return True
    return False


def delete_group(group_id):
    groups = list_groups()
    new = [g for g in groups if g.get("id") != group_id]
    if len(new) == len(groups):
        return False
    _save(new)
    log("deleted group %s" % group_id, "info")
    return True


def _key(member):
    return (member.get("source"), member.get("url"))


def is_member(group_id, source_id, url):
    g = get_group(group_id)
    if not g:
        return False
    return any(_key(m) == (source_id, url) for m in g.get("members", []))


def set_members(group_id, members):
    """Replace a group's membership wholesale. `members` is a list of
    {"source","url"} dicts (extra keys like "name" are kept)."""
    groups = list_groups()
    for g in groups:
        if g.get("id") == group_id:
            # De-dup by (source, url), preserve order.
            seen, clean = set(), []
            for m in members:
                k = _key(m)
                if k[0] and k[1] and k not in seen:
                    seen.add(k)
                    clean.append({"source": m.get("source"), "url": m.get("url"),
                                  "name": m.get("name", "")})
            g["members"] = clean
            _save(groups)
            return True
    return False


def add_member(group_id, source_id, url, name=""):
    g = get_group(group_id)
    if not g:
        return False
    members = g.get("members", [])
    if any(_key(m) == (source_id, url) for m in members):
        return False
    members.append({"source": source_id, "url": url, "name": name})
    return set_members(group_id, members)


def remove_member(group_id, source_id, url):
    g = get_group(group_id)
    if not g:
        return False
    members = [m for m in g.get("members", []) if _key(m) != (source_id, url)]
    return set_members(group_id, members)
