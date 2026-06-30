# -*- coding: utf-8 -*-
"""Local, cross-platform subscriptions.

This mirrors Grayjay's core idea: you subscribe to a *channel* inside the app,
not on the upstream platform. Subscriptions are stored locally and aggregated
across every installed source, so "Subscriptions" shows the newest content from
all the creators you follow regardless of which platform they're on.

Stored as JSON at <profile>/subscriptions.json:
    [{"source": "<source id>", "url": "<channel url>",
      "name": "<channel name>", "thumbnail": "<url>"}, ...]
A subscription is keyed by (source, url).
"""
import json
import os

from ..kodiutils import profile_path, log


def _path():
    return os.path.join(profile_path(), "subscriptions.json")


def list_subscriptions():
    p = _path()
    if not os.path.isfile(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        log("failed to read subscriptions: %s" % exc, "warning")
        return []


def _save(subs):
    with open(_path(), "w", encoding="utf-8") as fh:
        json.dump(subs, fh, indent=2)


def is_subscribed(source_id, url):
    return any(s.get("source") == source_id and s.get("url") == url
               for s in list_subscriptions())


def add_subscription(source_id, url, name="", thumbnail=""):
    subs = list_subscriptions()
    if any(s.get("source") == source_id and s.get("url") == url for s in subs):
        return False
    subs.append({"source": source_id, "url": url,
                 "name": name or url, "thumbnail": thumbnail})
    _save(subs)
    log("subscribed: %s (%s)" % (name or url, source_id), "info")
    return True


def remove_subscription(source_id, url):
    subs = list_subscriptions()
    new = [s for s in subs if not (s.get("source") == source_id and s.get("url") == url)]
    if len(new) == len(subs):
        return False
    _save(new)
    log("unsubscribed: %s (%s)" % (url, source_id), "info")
    return True
