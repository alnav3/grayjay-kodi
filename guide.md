# Grayjay for Kodi — install & run guide

`plugin.video.grayjay` is a Kodi addon that runs [Grayjay](https://grayjay.app)
JavaScript source plugins inside Kodi. This guide covers the **non-Kodi-store**
install paths that the addon supports today.

> **TL;DR**
> ```sh
> # From inside this repo:
> ln -sfn "$(pwd)" ~/.kodi/addons/plugin.video.grayjay   # or wherever your Kodi userdir lives
> nix run .#kodi                                          # or run kodi normally
> ```
> First launch will download `quickjs` once (~30 s) into
> `~/.local/share/grayjay-kodi/python-deps/` and export it via `PYTHONPATH`
> so the addon can use the modern JS engine.

---

## 1. What you need

| Requirement        | Notes                                                                                   |
| ------------------ | --------------------------------------------------------------------------------------- |
| Kodi 20.x or 21.x  | The addon uses `xbmc.python` 3.0.0                                                      |
| `inputstream.adaptive` | Required to play YouTube (it consumes the DASH MPD served by the addon)             |
| Internet access    | Sources fetch content from the web; the addon also calls a signature-update endpoint   |
| A JS engine        | The addon prefers `quickjs`. Without it, only the vendored pure-Python `js2py` works — that's **ES5 only**, so most modern source plugins will fail |

### About the JS engine

The addon tries engines in this order (see `resources/lib/engine/jsengine.py`):

1. `quickjs` — modern (ES2020), supports synchronous host callbacks, what real signed plugins need.
2. `py_mini_racer` — V8, eval-only, no host callbacks; can run the harness but **cannot** drive HTTP-using plugins.
3. `js2py` (vendored) — pure Python, ES5 only. Will parse the bundled `tools/example_source` and trivial test sources, **not** signed community plugins like YouTube or PeerTube.

On Python 3.12+ the vendored `js2py` is also completely broken (its bytecode
introspection trips on the new compiler). So on any modern Kodi you **must**
have `quickjs` available for the addon to do anything useful.

nixpkgs' `python3Packages.quickjs` is currently flagged insecure (CVE-batches),
so the nix flake avoids it. The bootstrap installs a fresh PyPI wheel via pip
instead. If you don't want that, skip the bootstrap and use js2py (and you
won't be able to load real signed plugins).

---

## 2. Pick your install path

There are three paths, in increasing order of automation:

| Path                       | When to use                                                                          |
| -------------------------- | ------------------------------------------------------------------------------------ |
| **A. Nix flake**           | You're on NixOS or have Nix installed; you want the addon + a managed Kodi together |
| **B. `nix-shell -p kodi`** | You want minimal Nix involvement, just enough to launch Kodi + the addon locally     |
| **C. Manual drop-in**      | You already have Kodi installed (your distro's package, an AppImage, etc.) and just want to drop the addon into it |

Pick one.

---

## 3. Path A — Nix flake

The flake builds the addon as a proper Nix package and provides a dev shell
plus a Kodi launcher that wires everything up.

```sh
git clone https://github.com/grayjay-kodi/grayjay-kodi
cd grayjay-kodi

# Just want to run Kodi with the addon linked:
nix run .#kodi
```

On first launch, the wrapper:

1. Symlinks the repo to `~/.kodi/addons/plugin.video.grayjay` (skipped if it already exists).
2. Bootstraps `pip` + `quickjs` into `~/.local/share/grayjay-kodi/` (one-time, ~30 s).
3. Sets `PYTHONPATH` and `SSL_CERT_FILE`.
4. Execs Kodi.

If you want to develop:

```sh
nix develop               # opens a dev shell with kodi, python311, etc.
                          # The shellHook auto-links the addon into ~/.kodi/addons
                          # and bootstraps pip + requests + quickjs for off-Kodi use.

# Then inside the shell:
python3 tools/harness.py tools/example_source/config.json getHome
```

You can also build the addon as a standalone Nix package:

```sh
nix build .#default          # result/share/kodi/addons/plugin.video.grayjay/
nix profile install .#default  # puts it on your profile
```

### `KODI_HOME`

Kodi's user data directory is `~/.kodi/` by default. If you relocated it
(`KODI_HOME=/some/where kodi`), the wrapper honors that:

```sh
KODI_HOME=/var/kodi nix run .#kodi
```

---

## 4. Path B — plain `nix-shell -p kodi`

No flake needed, just the inputs.

```sh
nix-shell -p kodi python311

# Inside the shell:
python3 -m ensurepip --upgrade  # may warn; harmless
python3 -m pip install --user --break-system-packages requests quickjs

# Link the addon into Kodi's user-dir:
mkdir -p ~/.kodi/addons
ln -sfn "$(pwd)" ~/.kodi/addons/plugin.video.grayjay

# Run Kodi:
kodi
```

This mirrors what the flake does, but spelled out. The off-Kodi test harness:

```sh
python3 tools/harness.py tools/example_source/config.json getHome
```

---

## 5. Path C — drop it into an existing Kodi

You have Kodi installed (system package, AppImage, whatever) and just want the
addon. Three steps:

### 5.1 Copy or symlink the addon

```sh
# Default Kodi user data location:
KODI_USER=~/.kodi                       # Linux
# KODI_USER=~/Library/Application Support/Kodi   # macOS
# KODI_USER=%APPDATA%\Kodi              # Windows

ln -sfn /path/to/grayjay-kodi \
        "$KODI_USER/addons/plugin.video.grayjay"
```

A symlink is fine and lets you `git pull` to update.

### 5.2 Make sure `quickjs` is importable

The addon needs `quickjs` (the Python wheel, [pypi.org/project/quickjs](https://pypi.org/project/quickjs/))
on Kodi's embedded Python's import path. KDE/Flatpak/AppImage Kodis ship their
own Python — typically `python3.11` or `python3.13` — that lives next to the
`kodi` binary. Find it like so:

```sh
KODI_PY=$(dirname "$(readlink -f "$(which kodi)")")/../lib/python3*/bin/python3
echo "$KODI_PY"   # verify
"$KODI_PY" --version
```

Then install `quickjs` into a writable target directory that you can prepend to
`PYTHONPATH`:

```sh
QUICKJS_DEPS=$HOME/.local/share/grayjay-kodi/python-deps
mkdir -p "$QUICKJS_DEPS"
"$KODI_PY" -m pip install --target "$QUICKJS_DEPS" --break-system-packages quickjs
```

(`--break-system-packages` is needed because Kodi's Python is "externally
managed" — it's a vendor directory, not a real venv.)

### 5.3 Launch Kodi with the right env

```sh
export PYTHONPATH="$HOME/.local/share/grayjay-kodi/python-deps${PYTHONPATH:+:$PYTHONPATH}"
kodi
```

If you don't want to remember this, wrap it in a one-liner:

```sh
# ~/bin/kodi-grayjay
#!/bin/sh
export PYTHONPATH="$HOME/.local/share/grayjay-kodi/python-deps${PYTHONPATH:+:$PYTHONPATH}"
exec kodi "$@"
```

### 5.4 Verify inside Kodi

1. Open Kodi.
2. **Settings → System → Logging** — turn on debug logging temporarily.
3. **Videos → Add-ons → Grayjay**.
4. If it appears: ✅ you're in.
5. If not, check `~/.kodi/temp/kodi.log` for `plugin.video.grayjay` errors. The most common one is the missing `quickjs` module — re-check step 5.2/5.3.

---

## 6. Using the addon

Once the addon is visible in Kodi:

1. Open **Videos → Add-ons → Grayjay**.
2. The first screen shows the root menu:
   - **Add source…** — paste a Grayjay config URL, or pick from FUTO's
     first-party plugins (Odysee, Rumble, PeerTube, Twitch, SoundCloud,
     Spotify, …).
   - Per installed source: browse / search / context-menu **Update**.
3. Each install fetches the config, downloads the script, verifies the
   signature, and persists both under `<userdir>/addons/<source-id>/`.
4. Browse. PeerTube is the most reliable end-to-end on the off-Kodi harness;
   YouTube also works but requires the `quickjs` engine and a populated
   session state (`saveState()` is cached in `<userdir>/plugin.video.grayjay/`).

### Sources

- **Signed sources**: signature is verified against the exact downloaded bytes
  before anything is persisted. Failed verify aborts cleanly. This mirrors
  FUTO's `SignatureProvider` (RSASSA-PKCS1-v1.5/SHA-512).
- **Unsigned sources**: accepted but logged as a security risk.
- **Auto-update**: a background service (`service.py`) re-fetches each source's
  `sourceUrl` periodically (default 24 h; configurable in Settings →
  Updates). Re-verifies signature before swapping files.
- **Manual update**: root menu → "Check for updates…", or per-source context
  menu → "Update".

---

## 7. Removing the addon

```sh
# Remove the symlink:
rm ~/.kodi/addons/plugin.video.grayjay

# Optional: wipe the installed sources + cached state:
rm -rf ~/.grayjay-kodi                      # off-Kodi profile (see kodiutils.py)
rm -rf ~/.local/share/grayjay-kodi          # the pip-installed quickjs deps
```

The `~/.kodi/addons/plugin.video.grayjay` symlink removal is enough for Kodi
to forget the addon; everything else is the host-side state.

---

## 8. Troubleshooting

| Symptom                                              | Likely cause                                                              | Fix                                                                                              |
| ---------------------------------------------------- | ------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Addon doesn't appear in Videos → Add-ons             | Wrong Kodi user-dir; symlink is in `~/.local/share/kodi/...` instead of `~/.kodi/...` | Confirm where Kodi looks (`Settings → System → Profiles` or `kodi --userdata`); re-symlink there |
| Addon appears but clicking it shows "no Python module named quickjs" | Kodi's Python can't find the engine wheel; missing or wrong `PYTHONPATH`              | Re-do 5.2/5.3. Or `python3 -c "import quickjs"` from Kodi's Python directly to verify           |
| `your python version made changes to the bytecode`   | js2py falling back; you're on Python ≥ 3.12                                | Install `quickjs` — js2py is unfixable on 3.12+                                                 |
| YouTube won't play                                   | Missing `inputstream.adaptive`; or signature cipher fetch blocked         | Install ISA from Kodi's repo; check `~/.kodi/temp/kodi.log` for HTTP failures                    |
| Source install hangs                                 | Network issue or bad signature                                            | Look at `kodi.log` for `Signature verification FAILED` or DNS errors                              |
| Auto-update never runs                               | Disabled or interval not reached                                          | Settings → Updates → enable + check interval                                                     |

Kodi's log on Linux is at `~/.kodi/temp/kodi.log`; enable debug logging under
Settings → System → Logging for the addon's debug lines.

---

## 9. Layout of this repo

```
default.py              Kodi entry point (xbmc.python.pluginsource)
service.py              background service: auto-update + DASH manifest loopback HTTP
addon.xml               Kodi addon metadata
flake.nix               this repo's flake (packages + devShell + app)
guide.md                this file
tools/
  harness.py            off-Kodi test driver (no xbmc* needed)
  example_source/       offline source plugin for the harness
  dom_source/           DOMParser test source
  net_source/           HTTP-using test source
resources/lib/
  router.py             plugin:// → Kodi ListItems / player
  kodiutils.py          xbmc* wrappers
  sources/              install / update / signature verification
  engine/               JS engine + bridge + SDK prelude
  playback/             DASH MPD synthesis + loopback HTTP
  crypto/               RSASSA-PKCS1-v1_5 verify
```