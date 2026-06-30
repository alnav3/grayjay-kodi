# -*- coding: utf-8 -*-
"""Build a static DASH MPD from a set of YouTube adaptive formats.

Modern YouTube (and the Grayjay YouTube plugin) serve *adaptive* streams —
separate video-only and audio-only tracks — designed to be muxed by the
player. Kodi plays these via `inputstream.adaptive` given a DASH manifest, so
we synthesise one here.

Input is a list of "adaptiveFormats" dicts as YouTube returns them (itag,
mimeType, url, bitrate, width/height/fps, initRange/indexRange,
contentLength, audioSampleRate, audioChannels). Only formats with a *direct*
`url` and byte-range (`initRange`+`indexRange`, i.e. a `sidx` we can address
via SegmentBase) are usable; the rest are skipped.

The googlevideo URLs returned for the ANDROID_VR client are pre-signed and
honour HTTP Range requests, so SegmentBase byte-range addressing works without
any cipher/UMP handling on our side.
"""
import re
from collections import OrderedDict


def _codecs(mime):
    m = re.search(r'codecs="([^"]+)"', mime or "")
    return m.group(1) if m else ""


def _base_mime(mime):
    return (mime or "").split(";")[0].strip()


def _range(fmt, key):
    r = fmt.get(key) or {}
    start, end = r.get("start"), r.get("end")
    if start is None or end is None:
        return None
    return "%s-%s" % (start, end)


def _esc(url):
    # Only & is illegal in XML text/attribute among URL chars we emit.
    return (url or "").replace("&", "&amp;")


def _duration_iso(ms):
    secs = (ms or 0) / 1000.0
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = secs % 60
    return "PT%dH%dM%.3fS" % (h, m, s)


def usable_formats(formats):
    """Keep only direct-URL formats we can address with SegmentBase ranges."""
    out = []
    for f in formats or []:
        if not f.get("url"):
            continue
        if _range(f, "initRange") is None or _range(f, "indexRange") is None:
            continue
        out.append(f)
    return out


def build_mpd(formats, duration_ms=None):
    """Return a DASH MPD string, or None if no usable formats are present.

    `formats` is YouTube's adaptiveFormats list. Video/audio are split into
    AdaptationSets by base MIME type (a set must be codec-homogeneous), and
    duplicate itags within a set are dropped (Representation ids must be
    unique).
    """
    usable = usable_formats(formats)
    if not usable:
        return None

    if not duration_ms:
        duration_ms = max((int(f.get("approxDurationMs") or 0) for f in usable),
                          default=0)

    # group -> OrderedDict keyed by itag (dedup, stable order)
    groups = OrderedDict()
    for f in usable:
        base = _base_mime(f.get("mimeType"))
        typ = "audio" if base.startswith("audio") else "video"
        key = (typ, base)
        groups.setdefault(key, OrderedDict())[str(f.get("itag"))] = f

    sets = []
    for set_id, ((typ, base), reps_by_itag) in enumerate(groups.items()):
        reps = []
        for itag, f in reps_by_itag.items():
            codecs = _codecs(f.get("mimeType"))
            bw = int(f.get("bitrate") or f.get("averageBitrate") or 0)
            seg = ('<SegmentBase indexRange="%s"><Initialization range="%s"/>'
                   '</SegmentBase>' % (_range(f, "indexRange"),
                                       _range(f, "initRange")))
            if typ == "video":
                reps.append(
                    '<Representation id="%s" bandwidth="%d" codecs="%s" '
                    'mimeType="%s" width="%s" height="%s" frameRate="%s">'
                    '<BaseURL>%s</BaseURL>%s</Representation>' % (
                        itag, bw, codecs, base, f.get("width"), f.get("height"),
                        f.get("fps") or 30, _esc(f.get("url")), seg))
            else:
                reps.append(
                    '<Representation id="%s" bandwidth="%d" codecs="%s" '
                    'mimeType="%s" audioSamplingRate="%s">'
                    '<AudioChannelConfiguration '
                    'schemeIdUri="urn:mpeg:dash:23003:3:audio_channel_configuration:2011" '
                    'value="%s"/><BaseURL>%s</BaseURL>%s</Representation>' % (
                        itag, bw, codecs, base, f.get("audioSampleRate") or 48000,
                        f.get("audioChannels") or 2, _esc(f.get("url")), seg))
        if not reps:
            continue
        sets.append(
            '<AdaptationSet id="%d" contentType="%s" mimeType="%s" '
            'subsegmentAlignment="true" startWithSAP="1">%s</AdaptationSet>' % (
                set_id, typ, base, "".join(reps)))

    if not sets:
        return None

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" '
        'profiles="urn:mpeg:dash:profile:isoff-on-demand:2011" type="static" '
        'mediaPresentationDuration="%s" minBufferTime="PT1.5S">'
        '<Period>%s</Period></MPD>' % (_duration_iso(duration_ms), "".join(sets)))
