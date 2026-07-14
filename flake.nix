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
      # We deliberately do NOT pull anything from nixpkgs' python311Packages
      # other than the bare interpreter: in current nixpkgs (26.11pre) many
      # of those packages transitively require `sphinx-9.1.0`, which is
      # marked `disabled = pythonOlder "3.12"` and so cannot be evaluated on
      # 3.11. The shellHook therefore bootstraps `requests` (and optionally
      # `quickjs`) via pip instead.
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

              # Bootstrap pip + runtime deps. Nix's python311 ships without
              # pip; ensurepip fails because the interpreter doesn't allow
              # site-packages writes. Bootstrap pip via get-pip.py into the
              # user site, then use it to install `requests` and (optionally)
              # `quickjs` for ES2020 support.
              #
              # Both are optional: the addon falls back to urllib without
              # `requests`, and to the vendored js2py (ES5 only) without
              # `quickjs`. The bundled `tools/example_source` only needs js2py.
              if ! python3 -c "import pip" 2>/dev/null; then
                echo "[grayjay-kodi] bootstrapping pip via get-pip.py..."
                user_base=$(python3 -c "import site; print(site.getuserbase())")
                mkdir -p "$user_base"
                # Download get-pip.py directly into the user base.
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
                if ! python3 -c "import quickjs" 2>/dev/null; then
                  echo "[grayjay-kodi] installing 'quickjs' (user) for ES2020 support..."
                  python3 -m pip install --user --quiet --break-system-packages quickjs \
                    || echo "[grayjay-kodi] quickjs install failed; addon will use vendored js2py (ES5 only)"
                fi
              else
                echo "[grayjay-kodi] pip bootstrap failed; addon will use vendored js2py (ES5 only)"
              fi

              echo
              echo "[grayjay-kodi] python:        $(python3 --version) ($(which python3))"
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
      # addons/ directory AND with `quickjs` (the modern JS engine) installed
      # for Kodi's embedded Python, exported via PYTHONPATH.
      #
      # The vendored js2py (the only alternative engine in the addon) does
      # bytecode introspection that broke on Python 3.12+. nixpkgs' `quickjs`
      # is currently flagged insecure in this checkout, so we don't pull it
      # from there; instead we pip-install it (which has a fresh build) into
      # a per-user directory on first launch. Kodi's wrapper preserves
      # PYTHONPATH, so the addon then imports `quickjs` and skips js2py.
      # ------------------------------------------------------------------------
      apps = forAllSystems (system:
        let pkgs = pkgsFor system;
            # The python3 binary Kodi was built against — we use it to install
            # `quickjs` into a writable location so Kodi's embedded Python
            # (which links the same libpython) can import it via PYTHONPATH.
            kodiPython = "${pkgs.python3}/bin/python3";
        in {
          kodi = {
            type = "app";
            program = toString (pkgs.writeShellScript "run-kodi-with-grayjay" ''
              set -e
              addon_id=plugin.video.grayjay
              addon_src='${./.}'
              kodi_userdir="''${KODI_HOME:-$HOME/.kodi}"
              addon_dst="$kodi_userdir/addons/$addon_id"
              mkdir -p "$(dirname "$addon_dst")"
              if [[ ! -e "$addon_dst" ]]; then
                echo "[grayjay-kodi] linking addon -> $addon_dst"
                ln -s "$addon_src" "$addon_dst"
              fi

              # Make `quickjs` importable from Kodi's embedded Python. We
              # install it (and pip itself) once into a per-user dir, then
              # prepend that dir to PYTHONPATH so the addon finds it.
              kodi_py_deps="$HOME/.local/share/grayjay-kodi/python-deps"
              mkdir -p "$kodi_py_deps"
              if ! ${kodiPython} -c "import sys; sys.path.insert(0, '$kodi_py_deps'); import quickjs" 2>/dev/null; then
                echo "[grayjay-kodi] bootstrapping 'quickjs' for Kodi's Python (one-time)..."
                # Bootstrap pip into a sibling dir so we can use it.
                kodi_pip="$HOME/.local/share/grayjay-kodi/pip"
                if ! ${kodiPython} -c "import sys; sys.path.insert(0, '$kodi_pip'); import pip" 2>/dev/null; then
                  curl -sSL https://bootstrap.pypa.io/get-pip.py | ${kodiPython} - --quiet --target "$kodi_pip" --break-system-packages
                fi
                PYTHONPATH="$kodi_pip" ${kodiPython} -m pip install \
                  --target "$kodi_py_deps" --break-system-packages --quiet --no-deps quickjs
              fi

              export PYTHONPATH="$kodi_py_deps''${PYTHONPATH:+:$PYTHONPATH}"
              export SSL_CERT_FILE="${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              exec ${pkgs.kodi}/bin/kodi "$@"
            '');
            meta = {
              description = "Kodi with the Grayjay plugin (and quickjs engine) ready";
            };
          };
        });
    };
}