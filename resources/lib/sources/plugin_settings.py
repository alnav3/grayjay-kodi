# -*- coding: utf-8 -*-
"""Per-source plugin settings (Grayjay's `SourceV8PluginConfig.settings`).

Each source's config declares a `settings` array of descriptors, e.g.:
    {"variable": "allowAgeRestricted", "name": "Allow Age Restricted",
     "type": "Boolean", "default": "false", "description": "..."}
    {"variable": "sponsorBlockCat_Sponsor", "type": "Dropdown",
     "default": "1", "options": ["No skip", "Manual", "Automatic"], ...}

Grayjay passes the user's chosen values to `source.enable(config, settings, …)`
keyed by `variable`, typed: Boolean -> bool, Dropdown -> selected index (int),
everything else -> string. We persist the user's overrides next to the source
and merge them over the declared defaults at enable time.
"""
import json
import os


def _path(config):
    return os.path.join(config.base_dir, "settings.json")


def descriptors(config):
    """The list of setting descriptors declared by the source (may be empty)."""
    return config.raw.get("settings", []) or []


def _typed_default(desc):
    t = (desc.get("type") or "").lower()
    raw = desc.get("default")
    if t == "boolean":
        return str(raw).lower() == "true"
    if t == "dropdown":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0
    return raw if raw is not None else ""


def defaults(config):
    return {d["variable"]: _typed_default(d)
            for d in descriptors(config) if d.get("variable")}


def load(config):
    """Stored overrides merged over declared defaults -> {variable: value}."""
    values = defaults(config)
    try:
        with open(_path(config), "r", encoding="utf-8") as fh:
            values.update(json.load(fh))
    except (IOError, OSError, ValueError):
        pass
    return values


def save(config, values):
    with open(_path(config), "w", encoding="utf-8") as fh:
        json.dump(values, fh, indent=2)


def coerce(desc, value):
    """Coerce a raw user input to the type the plugin expects for `desc`."""
    t = (desc.get("type") or "").lower()
    if t == "boolean":
        return bool(value)
    if t == "dropdown":
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return value
