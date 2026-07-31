{
  description = "Grayjay for Kodi (plugin.video.grayjay) - a clean-room Python host for Grayjay JavaScript source plugins";

  inputs.nixpkgs.url = "nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      pkgsFor = system: import nixpkgs { inherit system; };
    in
    {
      legacyPackages = forAllSystems (system: pkgsFor system);
    } //
    {
      # ------------------------------------------------------------------------
      # packages.<system>.default
      #
      # A Kodi-addon "package" that drops the repo at exactly the layout Kodi
      # expects:
      #
      #     $out/share/kodi/addons/plugin.video.grayjay/
      #         addon.xml
      #         default.py
      #         service.py
      #         resources/...
      #
      # Install with:
      #     nix profile install .#default
      # and Kodi will pick it up on next start (assuming its user-dir points
      # here, see ./kodi-addons.nix / instructions below).
      # ------------------------------------------------------------------------
      packages = forAllSystems (system:
        let pkgs = pkgsFor system; in {
          default = pkgs.stdenvNoCC.mkDerivation {
            pname = "plugin.video.grayjay";
            version = "0.2.0";
            src = ./.;

            dontBuild = true;

            # Only ship the Kodi-addon subset of the repo. Drop build/dev files
            # (flake.*, tools/, .github/, .gitignore, README, LICENSE) so the
            # installed addon tree is identical to what a Kodi maintainer would
            # zip up.
            installPhase = ''
              runHook preInstall
              dst=$out/share/kodi/addons/plugin.video.grayjay
              mkdir -p "$dst"
              for f in addon.xml default.py service.py resources; do
                cp -R "$f" "$dst/"
              done
              chmod -R u+w "$dst"
              runHook postInstall
            '';

            meta = with pkgs.lib; {
              description = "Grayjay source plugins for Kodi (clean-room Python host)";
              longDescription = ''
                Kodi addon that runs Grayjay JavaScript source plugins inside
                Kodi. A pure-Python reimplementation of the Grayjay plugin host,
                so the same community source scripts used by the Grayjay app
                also run as Kodi plugins.
              '';
              homepage = "https://github.com/grayjay-kodi/grayjay-kodi";
              license = licenses.mit;
              platforms = platforms.unix;
            };
          };
        });

      # ------------------------------------------------------------------------
      # devShells.<system>.default
      #
      # Everything you need to:
      #   - run Kodi with this addon already linked into its user-dir
      #   - run `tools/harness.py` off-Kodi against any source plugin
      #
      # Python: pinned to 3.11. Kodi itself bundles 3.11.x, and the addon's
      # vendored `js2py` fallback (used when no native `quickjs` is available)
      # does bytecode introspection that broke in 3.12+. Using 3.11 here means
      # `tools/harness.py` and the addon share one interpreter version, so
      # off-Kodi tests behave exactly like the on-Kodi ones.
      #
      # JS engine: nixpkgs' `quickjs-ng` is symlinked into the addon's
      # vendor_native tree so the qjs_subprocess backend is used.  This avoids
      # the "InternalError: unconsistent stack size" bug in the pip `quickjs`
      # package (original QuickJS) on YouTube's rotating player bundle.
      #
      # The shellHook symlinks the addon into $XDG_DATA_HOME/kodi/addons the
      # first time the shell starts, so Kodi sees it without further wiring.
      # ------------------------------------------------------------------------
      devShells = forAllSystems (system:
        let pkgs = pkgsFor system;
            py = pkgs.python311;
        in {
          default = pkgs.mkShell {
            name = "grayjay-kodi-dev";
            packages = [
              pkgs.kodi
              py
              pkgs.cacert
              pkgs.quickjs-ng
            ];

            shellHook = ''
              export SSL_CERT_FILE="${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"

              # Make the pinned Python 3.11 take precedence over any system
              # python3 already on PATH. The vendored js2py is bytecode-fragile
              # on 3.12+.
              export PATH="${py}/bin:$PATH"

              # Nix's python wrapper sets PYTHONNOUSERSITE=true so that
              # `pip install --user` doesn't work. Unset it so we can bootstrap
              # pip + runtime deps into the user site on first entry.
              unset PYTHONNOUSERSITE

              addon_id=plugin.video.grayjay
              addon_src="$PWD"
              # Kodi's default user-profile on Linux is ~/.kodi/ regardless of
              # XDG_DATA_HOME. Honor $KODI_HOME for users who relocated it.
              kodi_userdir="''${KODI_HOME:-$HOME/.kodi}"
              addon_dst="$kodi_userdir/addons/$addon_id"

              if [[ ! -e "$addon_dst" ]]; then
                echo "[grayjay-kodi] linking addon -> $addon_dst"
                mkdir -p "$(dirname "$addon_dst")"
                ln -s "$addon_src" "$addon_dst"
              fi

              # Provide a quickjs-ng binary for the addon's qjs_subprocess
              # backend.  The pip `quickjs` package wraps original QuickJS
              # which hits "InternalError: unconsistent stack size" on
              # YouTube's rotating player bundle.  The addon already prefers
              # a vendored qjs_subprocess binary (jsengine.py) — we just need
              # to put one where it can find it for the current arch.
              machine="$(python3 -c 'import platform; print(platform.machine())')"
              qjs_bin="$(dirname "$(command -v qjs)")"
              qjs_vendor="$PWD/resources/lib/engine/vendor_native/$machine/bin"
              if [[ ! -x "$qjs_vendor/qjs" ]]; then
                mkdir -p "$qjs_vendor"
                ln -s "$qjs_bin/qjs" "$qjs_vendor/qjs"
                echo "[grayjay-kodi] symlinked quickjs-ng -> $qjs_vendor/qjs"
              fi

              # Bootstrap pip + runtime deps. Nix's python311 ships without
              # pip; ensurepip fails because the interpreter doesn't allow
              # site-packages writes. Bootstrap pip via get-pip.py into the
              # user site, then use it to install `requests`.
              #
              # `requests` is optional: the addon falls back to urllib without
              # it.  The JS engine no longer needs a pip package — quickjs-ng
              # is provided by nixpkgs (see qjs_vendor above).
              if ! python3 -c "import pip" 2>/dev/null; then
                echo "[grayjay-kodi] bootstrapping pip via get-pip.py..."
                user_base=$(python3 -c "import site; print(site.getuserbase())")
                mkdir -p "$user_base"
                if command -v curl >/dev/null 2>&1; then
                  curl -sSL https://bootstrap.pypa.io/get-pip.py -o "$user_base/get-pip.py" \
                    && python3 "$user_base/get-pip.py" --user --quiet --break-system-packages \
                    && rm -f "$user_base/get-pip.py" || true
                fi
                export PYTHONUSERBASE="$user_base"
                export PATH="$user_base/bin:$PATH"
              fi
              if python3 -c "import pip" 2>/dev/null; then
                if ! python3 -c "import requests" 2>/dev/null; then
                  echo "[grayjay-kodi] installing 'requests' (user)..."
                  python3 -m pip install --user --quiet --break-system-packages requests \
                    || echo "[grayjay-kodi] pip install failed; addon will fall back to urllib"
                fi
              else
                echo "[grayjay-kodi] pip bootstrap failed; addon will use urllib for HTTP"
              fi

              echo
              echo "[grayjay-kodi] python:        $(python3 --version) ($(which python3))"
              echo "[grayjay-kodi] qjs_subprocess: $(qjs --version 2>&1 || echo 'not found')"
              echo "[grayjay-kodi] addon linked:  $addon_dst"
              echo "[grayjay-kodi] run Kodi:      kodi"
              echo "[grayjay-kodi] off-Kodi test: python3 tools/harness.py tools/example_source/config.json getHome"
            '';
          };
        });

      # ------------------------------------------------------------------------
      # apps.<system>.kodi
      #
      # `nix run .#kodi` launches Kodi with the addon linked into its user
      # addons/ directory AND with a quickjs-ng binary available for the
      # addon's qjs_subprocess backend (avoids the pip quickjs bytecode
      # compiler bug).
      # ------------------------------------------------------------------------
      apps = forAllSystems (system:
        let pkgs = pkgsFor system;
            kodiPython = "${pkgs.python3}/bin/python3";
        in {
          kodi = {
            type = "app";
            program = toString (pkgs.writeShellScript "run-kodi-with-grayjay" ''
              set -e
              addon_id=plugin.video.grayjay
              # Use the writable working directory, not the read-only nix store
              # copy (${./.}).  We need to create the vendor_native symlink for
              # quickjs-ng, which isn't possible inside /nix/store.
              addon_src="$PWD"
              kodi_userdir="''${KODI_HOME:-$HOME/.kodi}"
              addon_dst="$kodi_userdir/addons/$addon_id"
              mkdir -p "$(dirname "$addon_dst")"
              if [[ ! -e "$addon_dst" ]]; then
                echo "[grayjay-kodi] linking addon -> $addon_dst"
                ln -s "$addon_src" "$addon_dst"
              fi

              # Provide a quickjs-ng binary for the addon's qjs_subprocess
              # backend (avoids the "unconsistent stack size" bug in the
              # pip quickjs package).
              machine=$(${kodiPython} -c 'import platform; print(platform.machine())')
              qjs_vendor="$addon_src/resources/lib/engine/vendor_native/$machine/bin"
              if [[ ! -x "$qjs_vendor/qjs" ]]; then
                mkdir -p "$qjs_vendor"
                ln -s "${pkgs.quickjs-ng}/bin/qjs" "$qjs_vendor/qjs"
                echo "[grayjay-kodi] symlinked quickjs-ng -> $qjs_vendor/qjs"
              fi

              export SSL_CERT_FILE="${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              exec ${pkgs.kodi}/bin/kodi "$@"
            '');
            meta = {
              description = "Kodi with the Grayjay plugin (and quickjs-ng engine) ready";
            };
          };
        });
    };
}