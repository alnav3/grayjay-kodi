/*
 * Grayjay plugin SDK scaffolding (Kodi host).
 *
 * This file is loaded into the JS engine BEFORE a source plugin script. It
 * reconstructs the globals a Grayjay source expects: the `source` object it
 * assigns its methods onto, result/model classes (PlatformVideo, pagers, ...),
 * the `http`/`utility` packages, and the `Type` constant tree.
 *
 * Anything that needs real I/O (HTTP, logging, crypto, base64) is delegated to
 * Python host callables registered by bridge.py under __host_* names. Host
 * calls exchange JSON strings to stay compatible with both quickjs and
 * py_mini_racer marshalling.
 */
(function (global) {
  "use strict";

  function hostCall(name, payload) {
    // __host_* are registered from Python; they take a JSON string and
    // return a JSON string.
    var res = global[name](JSON.stringify(payload || {}));
    return res ? JSON.parse(res) : null;
  }

  // ---- logging -----------------------------------------------------------
  global.log = function (msg) {
    try {
      hostCall("__host_log", { msg: typeof msg === "string" ? msg : JSON.stringify(msg) });
    } catch (e) {}
  };
  global.console = { log: global.log, warn: global.log, error: global.log, info: global.log };

  // ---- Type constants ----------------------------------------------------
  global.Type = {
    Source: { Dash: "DASH", HLS: "HLS", Video: "Video", Audio: "Audio" },
    Feed: { Videos: "VIDEOS", Streams: "STREAMS", Mixed: "MIXED", Live: "LIVE", Playlists: "PLAYLISTS" },
    Order: { Chronological: "CHRONOLOGICAL", Views: "VIEWS", Favorites: "FAVORITES" },
    Date: { LastHour: "LAST_HOUR", Today: "TODAY", LastWeek: "LAST_WEEK", LastMonth: "LAST_MONTH", LastYear: "LAST_YEAR" },
    Duration: { Short: "SHORT", Medium: "MEDIUM", Long: "LONG" },
  };

  // ---- HTTP package ------------------------------------------------------
  function HttpResponse(o) {
    this.url = o.url;
    this.code = o.code;
    this.headers = o.headers || {};
    this.body = o.body;
    this.isOk = o.code >= 200 && o.code < 300;
  }

  function Http(useAuth) {
    this._auth = !!useAuth;
  }
  Http.prototype._req = function (method, url, headers, body) {
    var o = hostCall("__host_http", {
      method: method,
      url: url,
      headers: headers || {},
      body: body === undefined ? null : body,
      useAuth: this._auth,
    });
    return new HttpResponse(o);
  };
  Http.prototype.GET = function (url, headers, useAuth) { return this._req("GET", url, headers, null); };
  Http.prototype.POST = function (url, body, headers, useAuth) { return this._req("POST", url, headers, body); };
  Http.prototype.request = function (method, url, headers, body) { return this._req(method, url, headers, body); };
  // requestWithBody / batch are commonly used; expose minimal forms.
  Http.prototype.requestWithBody = function (method, url, body, headers) { return this._req(method, url, headers, body); };

  global.http = new Http(false);
  global.packageHttp = { newClient: function (useAuth) { return new Http(useAuth); } };

  // ---- utility package ---------------------------------------------------
  global.utility = {
    toBase64: function (s) { return hostCall("__host_b64encode", { data: s }).out; },
    fromBase64: function (s) { return hostCall("__host_b64decode", { data: s }).out; },
    randomUUID: function () { return hostCall("__host_uuid", {}).out; },
    md5: function (s) { return hostCall("__host_md5", { data: s }).out; },
  };

  // ---- model classes -----------------------------------------------------
  function Thumbnail(url, quality) { this.url = url; this.quality = quality || 0; }
  function Thumbnails(list) { this.sources = list || []; }
  global.Thumbnail = Thumbnail;
  global.Thumbnails = Thumbnails;

  function PlatformID(platform, id, pluginId) {
    this.platform = platform; this.value = id; this.pluginId = pluginId;
  }
  global.PlatformID = PlatformID;

  function PlatformAuthorLink(id, name, url, thumbnail, subscribers) {
    this.id = id; this.name = name; this.url = url;
    this.thumbnail = thumbnail; this.subscribers = subscribers;
  }
  global.PlatformAuthorLink = PlatformAuthorLink;

  function PlatformContent(o) {
    o = o || {};
    this.id = o.id; this.name = o.name; this.thumbnails = o.thumbnails;
    this.author = o.author; this.datetime = o.datetime; this.url = o.url;
    this.shareUrl = o.shareUrl; this.contentType = o.contentType;
  }
  global.PlatformContent = PlatformContent;

  function PlatformVideo(o) {
    PlatformContent.call(this, o);
    this.contentType = 1; // VIDEO
    this.duration = o.duration; this.viewCount = o.viewCount;
    this.isLive = !!o.isLive;
  }
  global.PlatformVideo = PlatformVideo;

  function PlatformVideoDetails(o) {
    PlatformVideo.call(this, o);
    this.description = o.description;
    this.video = o.video;            // VideoSourceDescriptor
    this.rating = o.rating;
    this.subtitles = o.subtitles || [];
  }
  global.PlatformVideoDetails = PlatformVideoDetails;

  function PlatformChannel(o) {
    o = o || {};
    this.id = o.id; this.name = o.name; this.thumbnail = o.thumbnail;
    this.banner = o.banner; this.subscribers = o.subscribers;
    this.description = o.description; this.url = o.url; this.links = o.links;
  }
  global.PlatformChannel = PlatformChannel;

  // ---- stream source descriptors ----------------------------------------
  function VideoUrlSource(o) {
    this.type = "VideoUrlSource"; this.width = o.width; this.height = o.height;
    this.container = o.container; this.codec = o.codec; this.name = o.name;
    this.bitrate = o.bitrate; this.duration = o.duration; this.url = o.url;
  }
  function HLSSource(o) {
    this.type = "HLSSource"; this.name = o.name || "HLS"; this.url = o.url;
    this.duration = o.duration; this.priority = !!o.priority;
  }
  function DashSource(o) {
    this.type = "DashSource"; this.name = o.name || "DASH"; this.url = o.url;
    this.duration = o.duration;
  }
  function VideoSourceDescriptor(videoSources) { this.isUnMuxed = false; this.videoSources = videoSources || []; }
  global.VideoUrlSource = VideoUrlSource;
  global.HLSSource = HLSSource;
  global.DashSource = DashSource;
  global.VideoSourceDescriptor = VideoSourceDescriptor;

  // ---- pagers ------------------------------------------------------------
  function ContentPager(results, hasMore, context) {
    this.results = results || []; this.hasMore = !!hasMore; this.context = context || {};
  }
  ContentPager.prototype.nextPage = function () { this.results = []; this.hasMore = false; return this; };
  ContentPager.prototype.hasMorePagers = function () { return this.hasMore; };

  function VideoPager(results, hasMore, context) { ContentPager.call(this, results, hasMore, context); }
  VideoPager.prototype = Object.create(ContentPager.prototype);
  function ChannelPager(results, hasMore, context) { ContentPager.call(this, results, hasMore, context); }
  ChannelPager.prototype = Object.create(ContentPager.prototype);
  function CommentPager(results, hasMore, context) { ContentPager.call(this, results, hasMore, context); }
  CommentPager.prototype = Object.create(ContentPager.prototype);

  global.ContentPager = ContentPager;
  global.VideoPager = VideoPager;
  global.ChannelPager = ChannelPager;
  global.CommentPager = CommentPager;

  function PlatformComment(o) {
    o = o || {};
    this.contextUrl = o.contextUrl; this.author = o.author; this.message = o.message;
    this.rating = o.rating; this.date = o.date; this.replyCount = o.replyCount;
    this.context = o.context;
  }
  global.PlatformComment = PlatformComment;

  function ResultCapabilities(types, sorts, filters) {
    this.types = types || []; this.sorts = sorts || []; this.filters = filters || [];
  }
  global.ResultCapabilities = ResultCapabilities;

  // ---- the source object the plugin populates ----------------------------
  global.source = {};

  // bridge_call: invoked by the Python host to run a plugin method and return
  // a plain-data result. Pager objects are flattened to {results, hasMore}.
  global.__bridge_call = function (method, argsJson) {
    var args = argsJson ? JSON.parse(argsJson) : [];
    var fn = global.source[method];
    if (typeof fn !== "function") {
      throw new Error("source." + method + " is not implemented by this plugin");
    }
    var out = fn.apply(global.source, args);
    if (out && typeof out.hasMorePagers === "function") {
      return JSON.stringify({ __pager: true, results: out.results, hasMore: out.hasMore, context: out.context });
    }
    return JSON.stringify(out === undefined ? null : out);
  };

  // bridge.js (Python) sets global.plugin to the parsed config before the
  // plugin script runs, so scripts can read plugin.config / settings.
})(this);
