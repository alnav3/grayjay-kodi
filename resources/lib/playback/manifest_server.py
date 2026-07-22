# -*- coding: utf-8 -*-
"""A tiny localhost HTTP server for DASH playback: manifests + media proxy.

inputstream.adaptive fetches the manifest through its own CURL downloader and
(on current builds) refuses a local-file path — it wants a real HTTP URL whose
response carries a `Content-Type`. So we serve the generated `.mpd` files from
the addon cache over `http://127.0.0.1:<port>/`.

The same server also relays the *media* segments (`/s/<sig>/<token>`). This is
not just convenience: googlevideo does not reliably honor the HTTP `Range`
header on the direct adaptive-format URLs — the canonical byte-range mechanism
for YouTube DASH itags is the `range=start-end` *query parameter* (what
YouTube's own player and yt-dlp use). When ISA seeks, it issues `Range`
requests; if the CDN ignores the header and streams from byte 0, that track
keeps playing as if no seek happened (video/audio desync) or playback aborts
outright. The proxy translates each `Range` header into a `range=` query
parameter and synthesizes the proper `206 Partial Content` reply, so seeking
works identically for both the video and the audio track.

The server runs inside the persistent background service (service.py); the
plugin process (a separate, short-lived process) discovers the port via a small
file in the profile dir. Only loopback is bound, only `*.mpd` files inside the
cache directory are served, and media URLs must carry a valid HMAC (signed with
a per-install key in the profile dir) — the proxy only relays URLs this addon
issued, so it is not an open relay for other local processes.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import threading

try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:  # pragma: no cover - Py<3.7
    from http.server import BaseHTTPRequestHandler
    from socketserver import ThreadingMixIn
    from http.server import HTTPServer

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

try:
    import requests as _requests
except ImportError:
    _requests = None

import urllib.request as _urlreq

from ..kodiutils import resolve_ca_bundle

_CA_BUNDLE = resolve_ca_bundle()

PORT_FILE = "manifest_port"
KEY_FILE = "proxy_key"

_CHUNK = 64 * 1024
_RANGE_RE = re.compile(r"^bytes=(\d+)-(\d*)")

# Match the bridge's default desktop UA so media requests look like the same
# client that made the player request.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36")


# -- signed media URLs ------------------------------------------------------
def proxy_secret(profile_dir):
    """Per-install HMAC key shared by the plugin process (which signs media
    URLs into the manifest) and the service (which verifies them)."""
    path = os.path.join(profile_dir, KEY_FILE)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
        if key:
            return key.encode("ascii")
    except (IOError, OSError):
        pass
    key = os.urandom(32).hex()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(key)
    return key.encode("ascii")


def _sign(secret, token):
    return hmac.new(secret, token.encode("ascii"), hashlib.sha256).hexdigest()[:32]


def _pack_token(payload):
    """Pack a JSON payload as a URL-safe base64 token (no padding)."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unpack_token(token):
    """Inverse of _pack_token; returns the decoded dict, or None on bad input."""
    try:
        padded = token + "=" * (-len(token) % 4)
        return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, TypeError):
        return None


def media_url(port, secret, url, content_length=0, mime=""):
    """Build a loopback proxy URL for an upstream media URL.

    The upstream URL, its total length (for Content-Range) and MIME type are
    packed into a base64url token and signed, so the server needs no shared
    state with the plugin process beyond the key file.
    """
    payload = {"u": url, "cl": int(content_length or 0), "m": mime or ""}
    token = _pack_token(payload)
    return "http://127.0.0.1:%d/s/%s/%s" % (port, _sign(secret, token), token)


def _decode_token(secret, sig, token):
    """Verify + decode a media token; None if the signature doesn't match.

    Media tokens must carry an `u` field pointing at an http(s) URL —
    anything else is a hostile or malformed payload. Subtitle tokens
    additionally allow an inline `t` field (raw body) and are decoded
    by `_decode_subtitle_token` instead."""
    if not hmac.compare_digest(_sign(secret, token), sig):
        return None
    payload = _unpack_token(token)
    if not payload:
        return None
    url = payload.get("u") or ""
    if not url.startswith("https://") and not url.startswith("http://"):
        return None
    return payload


def _decode_subtitle_token(secret, sig, token):
    """Verify + decode a /sub/ token. Either `u` (proxy upstream) or `t`
    (inline materialised body) must be set; http(s)-only for `u`."""
    if not hmac.compare_digest(_sign(secret, token), sig):
        return None
    payload = _unpack_token(token)
    if not payload:
        return None
    url = payload.get("u")
    text = payload.get("t")
    if url is None and text is None:
        return None
    if url is not None and not (url.startswith("https://") or
                                url.startswith("http://")):
        return None
    return payload


def subtitle_url(port, secret, url=None, text=None, mime="text/vtt",
                 language=None, name=None):
    """Build a loopback URL for a subtitle track that ISA will fetch as part
    of the DASH manifest.

    Two flavours:
      * url set, text None  — proxy the upstream URL (works for plain VTT
        endpoints; YouTube's manual `kind=vtt` tracks, etc.). Honours HTTP
        Range so a long subtitle can be seeked inside the player.
      * text set, url None  — the plugin materialised the body inline
        (auto-generated YouTube ASR, etc.). The token carries the raw VTT
        body; the server returns it directly with no upstream hop.

    Both kinds are signed with the same HMAC key as media URLs so the
    loopback server only serves what this addon issued.
    """
    if (url is None) == (text is None):
        raise ValueError("subtitle_url needs exactly one of url/text")
    payload = {
        "m": mime or "text/vtt",
        "lang": language or "",
        "name": name or "",
    }
    if url:
        payload["u"] = url
    else:
        payload["t"] = text
    token = _pack_token(payload)
    return "http://127.0.0.1:%d/sub/%s/%s" % (port, _sign(secret, token), token)


def _parse_range(header):
    """'bytes=a-b' / 'bytes=a-' -> (a, b|None); None if absent/unparseable."""
    if not header:
        return None
    m = _RANGE_RE.match(header.strip())
    if not m:
        return None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else None
    return (start, end)


def _open_upstream(url, headers):
    """GET an upstream URL; returns (code, headers, chunk-iterator, closer)."""
    if _requests is not None:
        r = _requests.get(url, headers=headers, stream=True,
                          timeout=(10, 30), verify=_CA_BUNDLE)
        return (r.status_code, r.headers,
                r.iter_content(chunk_size=_CHUNK), r.close)
    req = _urlreq.Request(url, headers=headers)
    r = _urlreq.urlopen(req, timeout=30)

    def chunks():
        while True:
            block = r.read(_CHUNK)
            if not block:
                return
            yield block

    return getattr(r, "status", r.getcode()), r.headers, chunks(), r.close


def _make_handler(cache_dir, profile_dir):
    class Handler(BaseHTTPRequestHandler):
        # Keep-alive matters here: ISA fetches every subsegment as its own
        # request, and a fresh TCP connection per ~few-hundred-KB chunk starves
        # the player on slow boxes.
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass  # stay out of the Kodi log

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path.startswith("/s/"):
                self._serve_media(path)
                return
            if path.startswith("/sub/"):
                self._serve_subtitle(path)
                return
            self._serve_manifest(path)

        # -- manifests ----------------------------------------------------
        def _serve_manifest(self, path):
            name = os.path.basename(path.lstrip("/"))
            if not name.endswith(".mpd"):
                self.send_error(404)
                return
            fs_path = os.path.join(cache_dir, name)
            if not os.path.isfile(fs_path):
                self.send_error(404)
                return
            try:
                with open(fs_path, "rb") as fh:
                    data = fh.read()
            except (IOError, OSError):
                self.send_error(500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/dash+xml")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # -- subtitle proxy --------------------------------------------------
        def _serve_subtitle(self, path):
            """Serve a subtitle body — either an inline VTT string (the plugin
            materialised it via getSubtitles()) or a proxied upstream URL.

            Honours HTTP Range on the inline case too: a player often asks for
            byte ranges even on small static files, and ISA in particular can
            issue an open-ended `bytes=N-` request when probing."""
            parts = path.split("/", 3)  # ['', 'sub', sig, token]
            if len(parts) != 4 or not parts[2] or not parts[3]:
                self.send_error(404)
                return
            payload = _decode_subtitle_token(proxy_secret(profile_dir),
                                             parts[2], parts[3])
            if payload is None:
                self.send_error(403)
                return

            mime = payload.get("m") or "text/vtt"
            inline = payload.get("t")

            if inline is not None:
                # Inline materialised subtitle body (raw bytes expected).
                if isinstance(inline, str):
                    inline = inline.encode("utf-8")
                self._send_bytes(inline, mime)
                return

            url = payload.get("u") or ""
            if not (url.startswith("https://") or url.startswith("http://")):
                self.send_error(400)
                return

            # Proxy the upstream URL with Range support (YouTube's timedtext
            # occasionally honors HTTP Range; the media proxy below does the
            # same range->googlevideo dance when needed).
            rng = _parse_range(self.headers.get("Range"))
            sent_range = None
            up_headers = {"User-Agent": _UA,
                          "Accept": "text/vtt,*/*;q=0.5"}
            if rng:
                start, end = rng
                if end is None:
                    end = ""
                sep = "&" if "?" in url else "?"
                url = "%srange=%d-%s" % (url + sep, start, end)
                sent_range = (start, end)
            try:
                code, up, chunks, close = _open_upstream(url, up_headers)
            except Exception:
                self.send_error(502)
                return
            try:
                if code not in (200, 206):
                    close()
                    self.send_error(code if 400 <= code < 600 else 502)
                    return
                length_hdr = up.get("Content-Length")
                if length_hdr is None:
                    data = b"".join(chunks)
                    chunks = (data,)
                    length = len(data)
                else:
                    length = int(length_hdr)
                content_range = None
                if sent_range is not None and code == 200:
                    status = 206
                    content_range = "bytes %d-%d/%d" % (
                        sent_range[0],
                        sent_range[0] + max(length - 1, 0),
                        length)
                elif code == 206:
                    status = 206
                    content_range = up.get("Content-Range")
                else:
                    status = 200
                self.send_response(status)
                self.send_header("Content-Type",
                                 mime or up.get("Content-Type") or
                                 "text/vtt")
                self.send_header("Content-Length", str(length))
                if content_range:
                    self.send_header("Content-Range", content_range)
                # Subtitles are tiny and Kodi/ISA sometimes re-fetches the
                # entire body on every seek. Allow long-lived caching keyed
                # on the signed URL.
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                for block in chunks:
                    if block:
                        self.wfile.write(block)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                try:
                    close()
                except Exception:
                    pass

        def _send_bytes(self, body, mime):
            """Write a small static body with optional Range support."""
            total = len(body)
            rng = _parse_range(self.headers.get("Range"))
            if rng:
                start, end = rng
                if end is None or end >= total:
                    end = total - 1
                if start >= total:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % total)
                    self.end_headers()
                    return
                body = body[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Range",
                                 "bytes %d-%d/%d" % (start, end, total))
            else:
                self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(body)

        # -- media proxy ----------------------------------------------------
        def _serve_media(self, path):
            parts = path.split("/", 3)  # ['', 's', sig, token]
            if len(parts) != 4 or not parts[2] or not parts[3]:
                self.send_error(404)
                return
            payload = _decode_token(proxy_secret(profile_dir), parts[2], parts[3])
            if payload is None:
                self.send_error(403)
                return
            url = payload["u"]
            total = int(payload.get("cl") or 0)
            mime = payload.get("m") or ""

            rng = _parse_range(self.headers.get("Range"))
            sent_range = None
            up_headers = {"User-Agent": _UA}
            if rng:
                start, end = rng
                if end is None and total:
                    end = total - 1
                if end is not None:
                    # The googlevideo way: byte range as a query parameter.
                    sep = "&" if "?" in url else "?"
                    url = "%srange=%d-%d" % (url + sep, start, end)
                    sent_range = (start, end)
                else:
                    # Open-ended with unknown length: header is all we have.
                    up_headers["Range"] = "bytes=%d-" % start

            try:
                code, up, chunks, close = _open_upstream(url, up_headers)
            except Exception:
                self.send_error(502)
                return
            try:
                if code not in (200, 206):
                    close()
                    self.send_error(code if 400 <= code < 600 else 502)
                    return
                length_hdr = up.get("Content-Length")
                if length_hdr is None:
                    data = b"".join(chunks)
                    chunks = (data,)
                    length = len(data)
                else:
                    length = int(length_hdr)

                content_range = None
                if sent_range is not None and code == 200:
                    # Upstream honored the range= param but replies 200; ISA
                    # asked with a Range header, so answer 206 on its behalf.
                    status = 206
                    content_range = "bytes %d-%d/%s" % (
                        sent_range[0], sent_range[0] + max(length - 1, 0),
                        str(total) if total else "*")
                elif code == 206:
                    status = 206
                    content_range = up.get("Content-Range")
                else:
                    status = 200

                self.send_response(status)
                self.send_header("Content-Type",
                                 mime or up.get("Content-Type") or
                                 "application/octet-stream")
                self.send_header("Content-Length", str(length))
                if content_range:
                    self.send_header("Content-Range", content_range)
                self.end_headers()
                for block in chunks:
                    if block:
                        self.wfile.write(block)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # player aborted the request (e.g. a seek) — normal
            finally:
                try:
                    close()
                except Exception:
                    pass

    return Handler


def start(cache_dir, profile_dir):
    """Start the manifest server on an ephemeral loopback port and publish it.

    Returns the bound port. Safe to call once from the service; raises only if
    the socket can't be bound (caller should treat that as "no MPD playback").
    """
    if not os.path.isdir(cache_dir):
        os.makedirs(cache_dir)
    proxy_secret(profile_dir)  # ensure the signing key exists up front
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(cache_dir, profile_dir))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, name="grayjay-manifest")
    thread.daemon = True
    thread.start()
    with open(os.path.join(profile_dir, PORT_FILE), "w", encoding="utf-8") as fh:
        fh.write(str(port))
    return server, port


def published_port(profile_dir):
    """Read the port the service published, or None."""
    try:
        with open(os.path.join(profile_dir, PORT_FILE), "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (IOError, OSError, ValueError):
        return None
