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

## The hard dependency: a JS engine in Kodi's Python

This is the make-or-break piece. Kodi runs its own bundled CPython, so you need
a JS engine importable from it:

- **`quickjs`** (preferred) — supports host callbacks, so HTTP works
  synchronously. Needs a build matching Kodi's Python ABI and your CPU arch
  (the CoreELEC test box is **aarch64**).
- **`py_mini_racer`** (fallback) — V8, but eval-only: no host callbacks, so
  HTTP-driven plugins won't work without a redesign.

Getting a working `quickjs` wheel onto the target is the main open task.

## Status / TODO

- [x] Addon scaffold, routing, source install/list/remove
- [x] JS engine abstraction + Grayjay SDK scaffolding (`packages.js`)
- [x] Host HTTP / log / base64 / uuid / md5 bridge
- [x] Off-Kodi test harness (`tools/harness.py`)
- [ ] Ship a `quickjs` build for Kodi/aarch64 (blocking real plugin runs)
- [ ] **Signature verification** (scriptSignature / scriptPublicKey) — currently
      scripts are **not** verified. See Security.
- [ ] `DOMParser` package (many plugins need it) — currently absent
- [ ] Pager `nextPage()` continuation across Kodi page loads
- [ ] Settings persistence per source, auth/login flows
- [ ] Comments, channels, playlists, search capabilities UI

## Security

Grayjay signs plugin scripts; **this host does not yet verify those
signatures**, and it grants plugins network access (scoped by the plugin's
declared `allowUrls`). Only install sources you trust until signing lands.

## Development

Run a plugin without Kodi:

```sh
python3 tools/harness.py /path/to/source/config.json getHome
```

This stubs the `xbmc*` modules and drives the bridge directly.
