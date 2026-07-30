# -*- coding: utf-8 -*-
"""Session registry for YouTube UMP/SABR fallback playback.

Some videos fail Android/Android-VR/iOS player-response verification (e.g.
"no audio streams"); the YouTube plugin itself then falls back to UMP/SABR
(YTABRExecutor, see resources/lib/engine/ump_shim.js), which has no static
per-segment URL -- segments are fetched live through a stateful executor that
must stay alive for the whole playback (it carries its own session cookies
and a sliding-window segment cache).

Kodi's `action=play` process is single-shot and exits right after
setResolvedUrl, so that liveness has to live in the persistent background
service instead. This module holds one PluginBridge (and its qjs subprocess)
per active UMP playback, keyed by a random session token embedded in the
manifest's media URLs, for as long as manifest_server.py is still getting
segment requests for it.
"""
import base64
import threading
import time
import uuid

from ..kodiutils import log

_SESSIONS = {}
_IDLE_TIMEOUT_S = 180
# bridge.call() blocks synchronously on the qjs subprocess pipe with no
# built-in cancellation. Bound every call so an unexpected hang wedges one
# qjs subprocess at most, never a whole service thread indefinitely.
# __getUmpManifests can call generate() on several candidate quality sources
# in sequence, each of which can (for a combined muxed source) legitimately
# take up to _read_capped's 45s -- observed ~85s wall-clock for a full
# resolution across ~12 candidate qualities, so this needs real headroom.
_CALL_TIMEOUT_S = 120
# A couple of retries as cheap resilience against a transient per-attempt
# failure (network blip, one candidate source erroring) -- not a workaround
# for anything structural, both the separate- and combined-source shapes
# getContentDetails can return now resolve successfully.
_MAX_RESOLVE_ATTEMPTS = 2
_RESOLVE_RETRY_DELAY_S = 3


def _call_with_timeout(bridge, method, args, timeout_s=_CALL_TIMEOUT_S):
    """bridge.call(method, args), killing the bridge's qjs subprocess (and
    raising TimeoutError) if it doesn't return in time."""
    result = {}
    def run():
        try:
            result["value"] = bridge.call(method, args)
        except Exception as exc:
            result["error"] = exc
    t = threading.Thread(target=run, name="ump-call-%s" % method, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        log("ump: %s timed out after %ss, killing bridge" % (method, timeout_s), "warning")
        bridge.close()  # unblocks/kills the stuck worker thread's pipe read
        raise TimeoutError("%s timed out" % method)
    if "error" in result:
        raise result["error"]
    return result.get("value")


def create_session(source_id, content_url):
    """Resolve a video's UMP-fallback sources and register a session.

    Returns {"token": str, "manifests": [...]} (see ump_shim.js's
    __getUmpManifests for the manifest shape), or None if the source doesn't
    exist or the video has nothing playable via UMP after _MAX_RESOLVE_ATTEMPTS
    attempts.
    """
    from ..engine.bridge import create_enabled_bridge
    for attempt in range(1, _MAX_RESOLVE_ATTEMPTS + 1):
        bridge = create_enabled_bridge(source_id)
        if bridge is None:
            return None
        try:
            _call_with_timeout(bridge, "getContentDetails", [content_url])
            manifests = _call_with_timeout(bridge, "__getUmpManifests", [])
        except Exception as exc:
            log("ump session resolve failed for %s (attempt %d/%d): %s"
                % (content_url, attempt, _MAX_RESOLVE_ATTEMPTS, exc), "warning")
            bridge.close()
            continue
        if manifests:
            token = uuid.uuid4().hex
            _SESSIONS[token] = {
                "bridge": bridge,
                "manifests": manifests,
                "last_used": time.time(),
            }
            if attempt > 1:
                log("ump: resolved on attempt %d/%d for %s"
                    % (attempt, _MAX_RESOLVE_ATTEMPTS, content_url), "info")
            return {"token": token, "manifests": manifests}
        bridge.close()
        if attempt < _MAX_RESOLVE_ATTEMPTS:
            time.sleep(_RESOLVE_RETRY_DELAY_S)
    log("ump: no UMP-playable sources after %d attempt(s) for %s"
        % (_MAX_RESOLVE_ATTEMPTS, content_url), "warning")
    return None


def fetch_segment(token, source_index, url_suffix):
    """Raw bytes for one UMP segment/init chunk, or None on any failure
    (unknown session, unknown source index, or the executor call itself
    erroring -- e.g. the server rotated tokens and the session needs a fresh
    resolve)."""
    sess = _SESSIONS.get(token)
    if sess is None:
        return None
    sess["last_used"] = time.time()
    try:
        result = _call_with_timeout(
            sess["bridge"], "__fetchUmpSegment", [source_index, url_suffix])
    except Exception as exc:
        log("ump segment fetch failed (token=%s idx=%s): %s"
            % (token, source_index, exc), "warning")
        _SESSIONS.pop(token, None)  # bridge is dead (closed by the timeout path) or unhealthy
        return None
    if not result or not result.get("ok"):
        log("ump segment fetch error (token=%s idx=%s): %s"
            % (token, source_index, (result or {}).get("error")), "warning")
        return None
    return base64.b64decode(result["base64"])


def evict_idle(max_age=_IDLE_TIMEOUT_S):
    """Close and drop sessions untouched for max_age seconds. Called from the
    background service's tick loop -- each session pins a live qjs
    subprocess, so idle ones need to be reaped rather than left running for
    the lifetime of the service."""
    now = time.time()
    stale = [tok for tok, sess in _SESSIONS.items()
             if now - sess["last_used"] > max_age]
    for tok in stale:
        sess = _SESSIONS.pop(tok, None)
        if sess is not None:
            try:
                sess["bridge"].close()
            except Exception:
                pass
    if stale:
        log("ump: evicted %d idle session(s)" % len(stale), "debug")
