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

By default the manifest carries exactly ONE video and ONE audio representation
(the best within the configured height cap). Advertising every harvested
format lets ISA's adaptation logic switch representations mid-play, and on
hardware decoders a representation switch means a decoder reinit — a ~1 second
black-out with an audio drop, recurring at the same (deterministic) points on
every replay of the same video. A single representation per track removes the
switch entirely; `adaptive=True` restores the old include-everything behaviour
for platforms that switch seamlessly.
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


def _height(f):
    return int(f.get("height") or 0)


def _fps(f):
    return int(f.get("fps") or 0)


def _bandwidth(f):
    return int(f.get("bitrate") or f.get("averageBitrate") or 0)


def _is_audio(f):
    return _base_mime(f.get("mimeType")).startswith("audio")


def _codec_rank(f):
    """Lower is better. Prefer H.264 — universally hardware-decoded on the
    Kodi targets we care about — then VP9, then AV1."""
    c = _codecs(f.get("mimeType")).lower()
    if c.startswith("avc") or c.startswith("h264"):
        return 0
    if c.startswith("vp9") or c.startswith("vp09"):
        return 1
    if c.startswith("av01"):
        return 2
    return 3


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


def select_formats(usable, max_height=0, adaptive=False):
    """Apply the height cap, then pick the representations to advertise.

    Default (adaptive=False): the single best video (highest height under the
    cap, preferring hardware-friendly codecs at equal height) and the single
    best audio (preferring audio/mp4 / AAC, then bitrate). With adaptive=True,
    everything under the cap is kept.
    """
    videos = [f for f in usable if not _is_audio(f)]
    audios = [f for f in usable if _is_audio(f)]
    if max_height and videos:
        capped = [f for f in videos if _height(f) <= max_height]
        # Nothing under the cap: keep the smallest we have rather than nothing.
        videos = capped or [min(videos, key=_height)]
    if adaptive:
        return videos + audios
    chosen = []
    if videos:
        videos.sort(key=lambda f: (_height(f), -_codec_rank(f), _fps(f), _bandwidth(f)))
        chosen.append(videos[-1])
    if audios:
        audios.sort(key=lambda f: (_base_mime(f.get("mimeType")) == "audio/mp4",
                                   _bandwidth(f)))
        chosen.append(audios[-1])
    return chosen


def build_mpd(formats, duration_ms=None, url_map=None, max_height=0,
              adaptive=False, subtitles=None):
    """Return a DASH MPD string, or None if no usable formats are present.

    `formats` is YouTube's adaptiveFormats list. Video/audio are split into
    AdaptationSets by base MIME type (a set must be codec-homogeneous), and
    duplicate itags within a set are dropped (Representation ids must be
    unique). `url_map(fmt)` — when given — rewrites each representation's
    BaseURL (used to route media through the loopback range proxy);
    `max_height`/`adaptive` drive representation selection (see
    select_formats).

    `subtitles` (optional) is a list of dicts with `name`, `url`, `format`,
    `language` and `default` fields. Each becomes a single Representation
    inside a `text/vtt` AdaptationSet so Kodi/ISA surfaces them on the
    player's subtitle menu; `default=True` marks one as initially on.
    """
    usable = usable_formats(formats)
    if not usable:
        return None

    if not duration_ms:
        duration_ms = max((int(f.get("approxDurationMs") or 0) for f in usable),
                          default=0)

    selected = select_formats(usable, max_height=max_height, adaptive=adaptive)

    # group -> OrderedDict keyed by itag (dedup, stable order)
    groups = OrderedDict()
    for f in selected:
        base = _base_mime(f.get("mimeType"))
        typ = "audio" if base.startswith("audio") else "video"
        key = (typ, base)
        groups.setdefault(key, OrderedDict())[str(f.get("itag"))] = f

    sets = []
    for set_id, ((typ, base), reps_by_itag) in enumerate(groups.items()):
        reps = []
        for itag, f in sorted(reps_by_itag.items(), key=lambda kv: _bandwidth(kv[1])):
            codecs = _codecs(f.get("mimeType"))
            bw = _bandwidth(f)
            url = url_map(f) if url_map else f.get("url")
            seg = ('<SegmentBase indexRange="%s"><Initialization range="%s"/>'
                   '</SegmentBase>' % (_range(f, "indexRange"),
                                       _range(f, "initRange")))
            if typ == "video":
                reps.append(
                    '<Representation id="%s" bandwidth="%d" codecs="%s" '
                    'mimeType="%s" width="%s" height="%s" frameRate="%s">'
                    '<BaseURL>%s</BaseURL>%s</Representation>' % (
                        itag, bw, codecs, base, f.get("width"), f.get("height"),
                        f.get("fps") or 30, _esc(url), seg))
            else:
                reps.append(
                    '<Representation id="%s" bandwidth="%d" codecs="%s" '
                    'mimeType="%s" audioSamplingRate="%s">'
                    '<AudioChannelConfiguration '
                    'schemeIdUri="urn:mpeg:dash:23003:3:audio_channel_configuration:2011" '
                    'value="%s"/><BaseURL>%s</BaseURL>%s</Representation>' % (
                        itag, bw, codecs, base, f.get("audioSampleRate") or 48000,
                        f.get("audioChannels") or 2, _esc(url), seg))
        if not reps:
            continue
        sets.append(
            '<AdaptationSet id="%d" contentType="%s" mimeType="%s" '
            'subsegmentAlignment="true" subsegmentStartsWithSAP="1" '
            'startWithSAP="1">%s</AdaptationSet>' % (
                set_id, typ, base, "".join(reps)))

    sub_set = _build_subtitle_set(subtitles)
    if sub_set:
        sets.append(sub_set)

    if not sets:
        return None

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" '
        'profiles="urn:mpeg:dash:profile:isoff-on-demand:2011" type="static" '
        'mediaPresentationDuration="%s" minBufferTime="PT1.5S">'
        '<Period>%s</Period></MPD>' % (_duration_iso(duration_ms), "".join(sets)))


def _build_subtitle_set(subtitles):
    """Wrap a list of subtitle dicts into a single DASH AdaptationSet.

    One AdaptationSet holds all tracks because they share a contentType/mimeType
    and Kodi's ISA UI surfaces them as a single menu the player toggles through.
    The first track marked `default=True` is the one Kodi turns on initially.
    """
    if not subtitles:
        return None
    valid = [s for s in subtitles
             if s and (s.get("url") or s.get("_text"))]
    if not valid:
        return None
    set_id = "subs"
    reps = []
    for i, s in enumerate(valid):
        url = _esc(s.get("url") or "")
        # id must be unique across the whole MPD; prefix with 'sub' to avoid
        # colliding with video/audio itags which are numeric. ISA also uses
        # this as the user-visible label when no <Label> element is given,
        # so we encode language + a short hint of the display name.
        lang = (s.get("language") or "und").strip()
        rid = "sub-%s-%d" % (lang.replace("-", "_"), i + 1)
        attrs = 'id="%s" bandwidth="256" codecs="wvtt"' % rid
        # ISA only needs BaseURL; SegmentBase/SegmentTemplate would be needed
        # for HLS-style segmented subtitles, but a single WebVTT body fits in
        # one fetch.
        reps.append(
            '<Representation %s>'
            '<BaseURL>%s</BaseURL>'
            '</Representation>' % (attrs, url))
    attrs = (
        'id="%s" contentType="text" mimeType="text/vtt" lang="%s"'
    ) % (set_id, _esc(valid[0].get("language") or "und"))
    # Mark the default track on the AdaptationSet level too: some players key
    # off this rather than the per-Representation flag.
    return ('<AdaptationSet %s default="%s" subsegmentAlignment="true" '
            'subsegmentStartsWithSAP="1">%s</AdaptationSet>' % (
                attrs,
                "true" if any(s.get("default") for s in valid) else "false",
                "".join(reps)))
