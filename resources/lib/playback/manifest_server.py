# -*- coding: utf-8 -*-
"""A tiny localhost HTTP server that hands DASH manifests to ISA.

inputstream.adaptive fetches the manifest through its own CURL downloader and
(on current builds) refuses a local-file path — it wants a real HTTP URL whose
response carries a `Content-Type`. So we serve the generated `.mpd` files from
the addon cache over `http://127.0.0.1:<port>/`.

The server runs inside the persistent background service (service.py); the
plugin process (a separate, short-lived process) discovers the port via a small
file in the profile dir. Only loopback is bound and only `*.mpd` files inside
the cache directory are served — no path traversal, no remote exposure.
"""
import os
import threading

try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:  # pragma: no cover - Py<3.7
    from http.server import BaseHTTPRequestHandler
    from socketserver import ThreadingMixIn
    from http.server import HTTPServer

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True


PORT_FILE = "manifest_port"


def _make_handler(cache_dir):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # stay out of the Kodi log

        def do_GET(self):
            name = os.path.basename(self.path.split("?", 1)[0].lstrip("/"))
            if not name.endswith(".mpd"):
                self.send_error(404)
                return
            path = os.path.join(cache_dir, name)
            if not os.path.isfile(path):
                self.send_error(404)
                return
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except (IOError, OSError):
                self.send_error(500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/dash+xml")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def start(cache_dir, profile_dir):
    """Start the manifest server on an ephemeral loopback port and publish it.

    Returns the bound port. Safe to call once from the service; raises only if
    the socket can't be bound (caller should treat that as "no MPD playback").
    """
    if not os.path.isdir(cache_dir):
        os.makedirs(cache_dir)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(cache_dir))
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
