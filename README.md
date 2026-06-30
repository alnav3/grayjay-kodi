# Grayjay for Kodi (`plugin.video.grayjay`)

**Experimental.** A Kodi addon that runs [Grayjay](https://grayjay.app) source
plugins inside Kodi, so you can reuse the same community source scripts that the
Grayjay app uses.

Grayjay sources are signed JavaScript files + a JSON config, executed against an
embedded JS engine. This addon reimplements that plugin host in Python so the
scripts run inside Kodi and their results are mapped to Kodi list items and the
Kodi player.

> This is a clean-room host. It does **not** bundle Grayjay or any source
> plugin — see `LICENSE.md`. Grayjay is source-available, not open source, so
> this addon is not eligible for the official Kodi repo.

## Architecture

```
default.py                     Kodi entry point
resources/lib/
  router.py                    plugin:// routing → Kodi ListItems / player
  kodiutils.py                 xbmc* wrappers (stubbable off-Kodi)
  sources/
    config.py                  SourceV8PluginConfig parsing
    manager.py                 install / list / remove sources
  engine/
    jsengine.py                pluggable JS backend (quickjs | py_mini_racer)
    packages.js                Grayjay SDK scaffolding (runs inside the engine)
    bridge.py                  host callables (http, log, crypto) + plugin driver
tools/harness.py               run a plugin off-Kodi for testing
```

### Request flow
1. `router` resolves the installed `SourceConfig`.
2. `PluginBridge` loads `packages.js` (defines `source`, models, `http`,
   `utility`, `Type`) then the plugin script (which populates `source.*`).
3. `bridge.call("getHome", [...])` runs `source.getHome(...)` in JS; pager
   results come back as JSON.
4. `router` turns results into `ListItem`s; `getContentDetails` yields a stream
   URL for `setResolvedUrl`.

## The JS engine (solved for the CoreELEC target)

Kodi runs its own bundled CPython, so we need a JS engine importable from it.
Backends, in preference order (`jsengine.py`, override via `GRAYJAY_JS_BACKEND`):

- **`quickjs`** (used on the box) — Bellard's QuickJS. Supports **ES2020**
  (`?.`/`??`) *and* host callbacks (so HTTP/DOM bridges work synchronously).
- **`py_mini_racer`** — V8, eval-only (no host callbacks); not used.
- **`js2py`** (pure-Python fallback) — vendored; runs ES5 only, so it handles
  the bundled test sources but **not** real modern plugins.

### Key target fact: the box is 32-bit ARM

The CoreELEC box reports `aarch64` from `uname` (64-bit kernel) but ships a
**32-bit `armv7l` userspace** — `python3` is a 32-bit ELF. So aarch64 wheels
never load. The working engine is the **`quickjs` `cp311` `armv7l` wheel from
piwheels** (Raspberry Pi's repo), vendored under
`resources/lib/engine/vendor_native/armv7l-cp311/`. It needs `libatomic`
(32-bit ARM lacks native 64-bit atomics); `jsengine.py` `ctypes`-preloads
`libatomic.so.1` before importing. No compiler/pip needed on the box.

## Status / TODO

- [x] Addon scaffold, routing, source install/list/remove
- [x] **One-click official sources** — "Add source" opens a submenu to either
      paste a config URL or pick from FUTO's first-party plugins (Odysee,
      Rumble, PeerTube, Twitch, SoundCloud, Spotify, …); list in
      `resources/lib/sources/official.py`. Installs are signature-verified and
      auto-update like any other source.
- [x] **Official Grayjay branding** — `resources/icon.png` is FUTO's 512×512
      app icon; `resources/fanart.png` is a 1080p board built from the official
      mark on the brand background.
- [x] JS engine abstraction + Grayjay SDK scaffolding (`packages.js`)
- [x] Host HTTP / log / base64 / uuid / md5 bridge
- [x] Off-Kodi test harness (`tools/harness.py`)
- [x] **Signature verification** — pure-Python RSASSA-PKCS1-v1.5/SHA-512,
      matching Grayjay's `SignatureProvider`. Verified against FUTO's own test
      vectors *and* the real signed YouTube plugin. Enforced at install + load.
- [x] **`DOMParser` package** — bs4 + soupsieve backed `domParser`/`DOMNode`
      (querySelector, getElementsBy*, attributes, ...), faithful to Grayjay's
      jsoup API. Validated on the CoreELEC box.
- [x] Installed + registered live in Kodi 21.3 on the target box
- [x] **Real JS engine working on the box** — quickjs (armv7l) with ES2020 +
      host callbacks (see above).
- [x] **Uses Grayjay's own `source.js` SDK prelude** (models, exceptions,
      pagers, `Type`, `URLSearchParams`) verbatim + host-injected packages
      (`http`, `utility`, `bridge`, `domParser`) and a `URL` polyfill.
- [x] **Runs real signed community plugins.** PeerTube: `getHome` → 20 real
      videos; `getContentDetails` → real HLS stream URL, end to end on the box.
- [~] **YouTube** — loads, enables (4.25 MB incl. bundled JSDOM) and runs:
      `search`, `searchSuggestions`, channels and video metadata all return real
      data. Required engine work: rewrite `\-` in `/u` regex classes to `\x2d`
      (quickjs is stricter than V8); `setTimeout`/`WeakRef`/`FinalizationRegistry`
      stubs; `URL` polyfill; `utility.md5String`; `bridge.isLoggedIn()`; full
      `http.batch().DUMMY` BatchBuilder (un-gates the session client). The
      signature **cipher (sig+nsig) solves** via FUTO's remote solver. Remaining
      blocker: playable stream formats come back empty — YouTube **PO Token /
      BotGuard** gating, separate from the cipher and the current industry-wide
      frontier (needs an external PO-token/BotGuard provider).
- [ ] Other per-plugin gaps: Rumble is bot-blocked (307); Odysee hits a missing
      host function. Lighter API plugins (PeerTube) work best.
- [x] **Cross-platform subscriptions** — subscribe to a channel *via this addon*
      (not the upstream platform); the "Subscriptions" feed aggregates newest
      content from all followed channels across every installed source. Right-
      click any video → Subscribe / Go to channel.
- [x] **Source auto-update** — a background service (`service.py`) re-fetches
      each source's config from its canonical `sourceUrl` (falling back to the
      URL it was installed from), and when the remote `version` is newer it
      downloads the new script, **re-verifies the signature, and only then swaps
      the files** — a bad signature or a failed fetch leaves the working copy
      untouched. Interval + on/off are in Settings → Updates; also triggerable
      manually via the root menu's "Check for updates…" and a per-source
      "Update" context-menu entry.
- [ ] Pager `nextPage()` continuation across Kodi page loads
- [ ] Settings persistence per source, auth/login flows
- [ ] Channels, playlists, search capabilities UI

### What runs today (verified on the CoreELEC box)

Under quickjs: signature verification (matches FUTO vectors + real plugins;
caught a CRLF bug), DOMParser, HTTP bridge, the full Grayjay `source.js` SDK,
and **real plugins end-to-end** — PeerTube returns real videos and a playable
HLS URL. Engine differences vs Grayjay's V8 remain per-plugin (e.g. YouTube's
bundled JSDOM regex; sites with bot protection).

## Security

Grayjay signs plugin scripts, and **this host verifies those signatures**
(pure-Python RSASSA-PKCS1-v1.5/SHA-512, matching FUTO's `SignatureProvider`)
against the exact downloaded bytes — enforced at install *and* on every
auto-update, before anything is written to disk. Unsigned sources are allowed
but logged as a risk. Plugins get network access scoped by their declared
`allowUrls`. Still: only install sources you trust.

## Development

Run a plugin without Kodi:

```sh
python3 tools/harness.py /path/to/source/config.json getHome
```

This stubs the `xbmc*` modules and drives the bridge directly.
