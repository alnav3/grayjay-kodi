# -*- coding: utf-8 -*-
"""Entry point for the Grayjay Kodi addon.

Kodi invokes this script with a plugin:// URL in sys.argv. We hand off to the
router which dispatches based on the `action` query parameter.
"""
import sys

from resources.lib.router import Router


def main():
    router = Router(sys.argv)
    router.dispatch()


if __name__ == "__main__":
    main()
