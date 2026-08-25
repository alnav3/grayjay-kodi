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


def _is_original_audio(f):
    """True if this format is the video's original audio (not a dubbed alt).

    YouTube tags each audio track with `audioTrack` (id like "original" or
    "dub_<lang>", plus a `audioIsDefault` flag) and stamps dubbed-auto tracks
    with `xtags=acont=dubbed-auto:lang=<code>`. The original/default track
    is what viewers expect when their locale doesn't match the dub; the
    bitrate-maximising sort below would otherwise happily pick a Hindi
    auto-dub over the English original because the dub often wins on
    bitrate."""
    track = f.get("audioTrack") or {}
    if track.get("id") == "original":
        return True
    if track.get("audioIsDefault") or f.get("audioIsDefault"):
        return True
    xtags = f.get("xtags") or ""
    if "dubbed-auto" in xtags or xtags.startswith("acont=dub"):
        return False
    return False


def _audio_lang(f):
    """Best-effort ISO 639-1 code for an audio track, or '' if unknown.

    The MPD AdaptationSet's `lang` attribute is what `inputstream.adaptive`
    matches against Kodi's `locale.audiolanguage` (default "original"); with
    no `lang` that setting is a no-op, so we always carry the tag when we
    can derive one. Prefer `audioTrack.id` (e.g. "dub_zh-Hans" -> "zh"), then
    the BCP-47 in `xtags`."""
    track = f.get("audioTrack") or {}
    tid = (track.get("id") or "").lower()
    if tid.startswith("dub_"):
        tid = tid[4:]
    if tid and tid != "original":
        return tid.split("-")[0].lower()
    xtags = f.get("xtags") or ""
    m = re.search(r"lang=([a-zA-Z-]+)", xtags)
    if m:
        return m.group(1).split("-")[0].lower()
    return ""


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
        audios.sort(key=lambda f: (not _is_original_audio(f),
                                   _base_mime(f.get("mimeType")) == "audio/mp4",
                                   _bandwidth(f)))
        chosen.append(audios[-1])
    return chosen


def build_mpd(formats, duration_ms=None, url_map=None, max_height=0,
              adaptive=False, captions=None, caption_url_map=None):
    """Return a DASH MPD string, or None if no usable formats are present.

    `formats` is YouTube's adaptiveFormats list. Video/audio are split into
    AdaptationSets by base MIME type (a set must be codec-homogeneous), and
    duplicate itags within a set are dropped (Representation ids must be
    unique). `url_map(fmt)` — when given — rewrites each representation's
    BaseURL (used to route media through the loopback range proxy);
    `max_height`/`adaptive` drive representation selection (see
    select_formats).

    `captions` is an optional list of {url, lang, name, auto} dicts (one
    per subtitle track, both uploader-supplied and YouTube's auto-generated
    ASR). They're emitted as a single `text/vtt` AdaptationSet with one
    Representation per language; `caption_url_map` rewrites each track's
    URL through the loopback proxy (HMAC-signed, same scheme as media).
    """
    usable = usable_formats(formats)
    if not usable:
        return None

    if not duration_ms:
        duration_ms = max((int(f.get("approxDurationMs") or 0) for f in usable),
                          default=0)

    # For the MPD itself we want every distinct audio track the player
    # response gave us (original + each dub), each in its own AdaptationSet
    # keyed by language, so inputstream.adaptive can actually honour
    # Kodi's `locale.audiolanguage=original` / explicit prefs. The
    # single-audio select_formats path is still used by the router to
    # decide which format's URL to sign for the proxy's first request,
    # but here we surface them all.
    videos = [f for f in usable if not _is_audio(f)]
    audios = [f for f in usable if _is_audio(f)]
    if max_height and videos:
        capped = [f for f in videos if _height(f) <= max_height]
        videos = capped or [min(videos, key=_height)]
    if not adaptive:
        if videos:
            videos.sort(key=lambda f: (_height(f), -_codec_rank(f), _fps(f), _bandwidth(f)))
            videos = [videos[-1]]
    selected = videos + audios

    # group -> OrderedDict keyed by (lang, itag) so dubbed and original
    # audio (same itag, different audioTrack.id / xtags) don't collapse.
    # Video tracks still dedupe purely by itag (no language to distinguish).
    # Original audio lives in its own slot (lang="") so we can mark the
    # whole AdaptationSet default="true" — inputstream.adaptive will then
    # pick it when no explicit language preference matches.
    groups = OrderedDict()
    for f in selected:
        base = _base_mime(f.get("mimeType"))
        typ = "audio" if base.startswith("audio") else "video"
        if typ == "audio":
            is_orig = _is_original_audio(f)
            if is_orig:
                lang = ""
            else:
                lang = _audio_lang(f) or "und"
            key = (typ, base, lang)
            sub_key = (lang, str(f.get("itag")))
        else:
            key = (typ, base, "")
            sub_key = str(f.get("itag"))
        groups.setdefault(key, OrderedDict())
        if sub_key not in groups[key]:
            groups[key][sub_key] = f

    # Put the original audio AdaptationSet first so ISA (which tends to
    # pick the first match) and any DASH parser that just walks sets in
    # order both land on it. The AdaptationSet also carries default="true"
    # for spec-compliant clients.
    audio_keys = [k for k in groups if k[0] == "audio"]
    other_keys = [k for k in groups if k[0] != "audio"]
    audio_keys.sort(key=lambda k: (0 if k[2] == "" else 1, k[2] or ""))
    ordered_keys = audio_keys + other_keys

    sets = []
    for set_id, key in enumerate(ordered_keys):
        typ, base, lang = key
        reps_by_sub = groups[key]
        reps = []
        for sub_key, f in sorted(reps_by_sub.items(),
                                 key=lambda kv: _bandwidth(kv[1])):
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
                        sub_key, bw, codecs, base, f.get("width"), f.get("height"),
                        f.get("fps") or 30, _esc(url), seg))
            else:
                track_lang = _audio_lang(f)
                if not track_lang and lang != "und" and lang:
                    track_lang = lang
                if track_lang:
                    rep_id = "%s-%s" % (f.get("itag"), track_lang)
                    track_label = " (%s)" % track_lang
                else:
                    rep_id = str(f.get("itag"))
                    track_label = ""
                rep_attrs = ' id="%s" bandwidth="%d" codecs="%s" mimeType="%s" audioSamplingRate="%s"' % (
                    _esc(rep_id), bw, codecs, base,
                    f.get("audioSampleRate") or 48000)
                reps.append(
                    '<Representation%s>'
                    '<AudioChannelConfiguration '
                    'schemeIdUri="urn:mpeg:dash:23003:3:audio_channel_configuration:2011" '
                    'value="%s"/><Label>%s</Label>'
                    '<BaseURL>%s</BaseURL>%s</Representation>' % (
                        rep_attrs, f.get("audioChannels") or 2,
                        _esc((f.get("audioTrack") or {}).get("displayName", "") + track_label),
                        _esc(url), seg))
        if not reps:
            continue
        lang_attr = ' lang="%s"' % _esc(lang) if lang and lang != "und" else ""
        is_default = (typ == "audio" and lang == "")
        default_attr = ' default="true"' if is_default else ""
        sets.append(
            '<AdaptationSet id="%d" contentType="%s" mimeType="%s"%s%s '
            'subsegmentAlignment="true" subsegmentStartsWithSAP="1" '
            'startWithSAP="1">%s</AdaptationSet>' % (
                set_id, typ, base, lang_attr, default_attr, "".join(reps)))

    if not sets:
        return None

    if captions:
        # DASH subtitle AdaptationSet. One text/vtt Representation per
        # language; Kodi/ISA expose them in the subtitle picker, marking
        # ASR-generated ones with the "(auto)" indicator when Label starts
        # with that prefix.
        seen = set()
        text_reps = []
        for cap in captions:
            url = (caption_url_map(cap) if caption_url_map
                   else cap.get("url"))
            if not url or url in seen:
                continue
            seen.add(url)
            lang = cap.get("lang") or "und"
            base_name = cap.get("name") or lang
            label = "%s (auto)" % base_name if cap.get("auto") else base_name
            role_attr = ' role="forced"' if cap.get("forced") else ""
            sub_id = "subs-%s" % lang
            text_reps.append(
                '<Representation id="%s" bandwidth="1000" codecs="wvtt" '
                'mimeType="text/vtt"%s>'
                '<Label>%s</Label>'
                '<BaseURL>%s</BaseURL></Representation>' % (
                    _esc(sub_id), role_attr, _esc(label), _esc(url)))
        if text_reps:
            sets.append(
                '<AdaptationSet id="%d" contentType="text" mimeType="text/vtt" '
                'subsegmentAlignment="true" subsegmentStartsWithSAP="1" '
                'startWithSAP="1">%s</AdaptationSet>' % (
                    len(sets), "".join(text_reps)))

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" '
        'profiles="urn:mpeg:dash:profile:isoff-on-demand:2011" type="static" '
        'mediaPresentationDuration="%s" minBufferTime="PT1.5S">'
        '<Period>%s</Period></MPD>' % (_duration_iso(duration_ms), "".join(sets)))
