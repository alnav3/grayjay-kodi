/*
 * UMP/SABR bridge shim -- appended after the plugin script in the SAME eval
 * as script.js (bridge.py's `load()`), because it needs `extractABR_VideoDescriptor`
 * and the returned source objects' methods, which are only reachable from
 * code sharing script.js's top-level lexical scope (its own `class`
 * declarations, e.g. YTABRExecutor, are NOT visible from a later, separate
 * eval() call -- only from within this same combined eval, or via method
 * calls on an instance captured here, since closures resolve by definition
 * site).
 *
 * getContentDetails only reaches its own UMP/ABR fallback when the plugin's
 * Android/Android VR/iOS verification fails (script.js's own fallback
 * chain), so this monkey-patch is a no-op for videos that already play via
 * the existing direct-URL/DASH-harvest paths.
 */
(function () {
  var __origExtractABR = extractABR_VideoDescriptor;
  globalThis.__umpLastDescriptor = null;
  extractABR_VideoDescriptor = function () {
    var d = __origExtractABR.apply(this, arguments);
    globalThis.__umpLastDescriptor = d;
    return d;
  };

  // extractABR_VideoDescriptor returns one of two shapes (source.js's
  // VideoSourceDescriptor / UnMuxVideoSourceDescriptor, discriminated by
  // isUnMuxed):
  //  - isUnMuxed=false: ONLY videoSources, each entry a self-contained
  //    YTABRAudioVideoSource (combined muxed audio+video -- its own
  //    generate() already returns a manifest with BOTH an audio and a video
  //    AdaptationSet, via generateAVDash). Exactly ONE of these must be used
  //    -- the real Grayjay client's own debug helper (testUMPCombined) picks
  //    exactly one candidate the same way. Splicing more than one together
  //    was tried and confirmed harmful: it silently duplicates the whole
  //    audio+video stream into the same DASH Period, which crashed Kodi's
  //    decoder (CBitstreamConverter::BitstreamAllocAndCopy SIGSEGV) because
  //    the two representations aren't switch-compatible.
  //  - isUnMuxed=true: separate videoSources (video-only) and audioSources
  //    (audio-only) arrays -- must pick exactly one of each, never just "the
  //    first N concatenated" (that can easily yield 2 video sources and 0
  //    audio, which is the "no audio" symptom this replaced).
  //
  // YTABRAudioVideoSource.generate() (fetchCombinedInitialHeaders) POSTs to a
  // `keepalive=yes` videoplayback URL that can legitimately take 30-40s to
  // respond (BotGuard/attestation verification delay server-side, not a
  // stall -- confirmed empirically). bridge.py's _read_capped bounds that
  // wait at 45s instead of blocking on true EOF (which this kind of
  // connection may never send).
  function pickBestVideo(list) {
    if (!list || !list.length) return null;
    var arr = list.slice().sort(function (a, b) { return (b.height || 0) - (a.height || 0); });
    var at720 = arr.filter(function (s) { return s.height === 720; });
    return (at720[0]) || arr[0];
  }

  function pickBestAudio(list) {
    if (!list || !list.length) return null;
    var arr = list.slice().sort(function (a, b) { return (b.bitrate || 0) - (a.bitrate || 0); });
    var original = arr.filter(function (s) { return s.original; });
    return original[0] || arr[0];
  }

  // Cached on the descriptor itself so repeated calls (manifest generation,
  // then later segment fetches) agree on the same instances -- required so
  // .generate()'s cached state (lastDash/initialHeader/etc, set on that
  // exact object) is reused rather than losing it to a freshly-picked one.
  function umpSources() {
    var d = globalThis.__umpLastDescriptor;
    if (!d) return [];
    if (d.__selectedUmpSources) return d.__selectedUmpSources;
    var selected;
    if (d.isUnMuxed) {
      var video = pickBestVideo(d.videoSources || []);
      var audio = pickBestAudio(d.audioSources || []);
      if (video) video.__umpKind = "video";
      if (audio) audio.__umpKind = "audio";
      selected = [video, audio].filter(Boolean);
    } else {
      var best = pickBestVideo(d.videoSources || []);
      if (best) best.__umpKind = "combined";
      selected = best ? [best] : [];
    }
    d.__selectedUmpSources = selected;
    return selected;
  }

  // Base64-encode a Uint8Array without blowing the call stack on
  // String.fromCharCode.apply for large (100KB+) segments.
  function bytesToBase64(bytes) {
    var bin = "";
    var chunk = 8192;
    for (var i = 0; i < bytes.length; i += chunk) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(bin);
  }

  function describeSource(s, mpd) {
    return {
      kind: s.__umpKind || "video",
      mpd: mpd,
      itag: s.itag,
      mimeType: (s.sourceObj && s.sourceObj.mimeType) || s.container || "",
      codec: s.codec,
      bitrate: s.bitrate,
      width: s.width,
      height: s.height,
    };
  }

  // Returns per-source manifest text (a minimal single-representation DASH
  // MPD with placeholder https://grayjay.internal/... segment URLs) plus
  // enough metadata for the caller to splice/rewrite it. Index in the
  // returned array is the `sourceIndex` __fetchUmpSegment expects (matches
  // umpSources()'s own indexing, since both call the same cached selection).
  // umpSources() always returns exactly one combined source OR exactly one
  // video + one audio source -- never more, so at most 2 generate() calls
  // (each up to _read_capped's 45s server-side, BotGuard/attestation delay)
  // ever happen per resolution.
  // generate() can in principle return a Promise (the plugin's own retry/
  // backoff, now that "Async" is advertised in bridge.supportedFeatures) --
  // await it via Promise.all so __bridge_call's {__async:true} pump handles
  // the wait.
  source.__getUmpManifests = function () {
    var sources = umpSources();
    if (!sources.length) return null;
    var mapped = sources.map(function (s) {
      var gen = s.generate(0, 65536);
      if (gen && typeof gen.then === "function") {
        return gen.then(function (mpd) { return describeSource(s, mpd); });
      }
      return describeSource(s, gen);
    });
    var anyAsync = mapped.some(function (r) { return r && typeof r.then === "function"; });
    return anyAsync ? Promise.all(mapped) : mapped;
  };

  // Reuses one YTABRExecutor per source (cached on the source object itself)
  // so playbackCookie/sessionZm/the executor's own segment cache persist
  // across calls -- required for UMP session continuity. urlSuffix is the
  // full remainder after the "https://grayjay.internal" origin (e.g.
  // "/audio/internal/segment.mp4?segIndex=5") -- a combined source's single
  // executor answers both /video/... and /audio/... paths (confirmed by the
  // plugin's own prefetch code calling executor.executeRequest with both),
  // so there's no need to special-case combined vs. separate here.
  source.__fetchUmpSegment = function (sourceIndex, urlSuffix) {
    var sources = umpSources();
    var s = sources[sourceIndex];
    if (!s) return { ok: false, error: "unknown UMP source index " + sourceIndex };
    if (!s.__executor) s.__executor = s.getRequestExecutor();
    var url = "https://grayjay.internal" + urlSuffix;
    try {
      var bytes = s.__executor.executeRequest(url, {});
      if (!bytes) return { ok: false, error: "no bytes for " + url };
      return { ok: true, base64: bytesToBase64(bytes) };
    } catch (e) {
      return { ok: false, error: String((e && e.stack) || e) };
    }
  };
})();
