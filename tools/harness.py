# -*- coding: utf-8 -*-
"""Off-Kodi test harness.

Drives a source plugin without a running Kodi by importing the bridge directly.
The xbmc* modules are absent here, so kodiutils/router degrade to print/stdout.

Usage:
    python3 tools/harness.py <config.json> <method> [json-args]

Examples:
    python3 tools/harness.py sources/example/config.json getHome
    python3 tools/harness.py sources/example/config.json getContentDetails '["https://x/y"]'
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from resources.lib.sources.config import SourceConfig  # noqa: E402
from resources.lib.engine.bridge import PluginBridge    # noqa: E402


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    cfg_path = sys.argv[1]
    method = sys.argv[2]
    args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else []

    base_dir = os.path.dirname(os.path.abspath(cfg_path))
    cfg = SourceConfig.from_dir(base_dir)
    print("Loaded source: %s v%s (id=%s)" % (cfg.name, cfg.version, cfg.id))

    bridge = PluginBridge(cfg)
    print("JS backend:", bridge.engine.backend)
    bridge.enable()
    result = bridge.call(method, args)
    print("--- %s result ---" % method)
    print(json.dumps(result, indent=2)[:4000])


if __name__ == "__main__":
    main()
