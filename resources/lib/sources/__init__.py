# Make `from resources.lib.sources import sub_feed_cache` work under
# Kodi's plugin runtime. Without this re-export, `from .sources import
# X, Y, sub_feed_cache` fails with
# "cannot import name 'sub_feed_cache' from 'resources.lib.sources'"
# because the package hasn't been told to expose the submodule as an
# attribute. Importing it here has the side effect of binding the name.
from . import sub_feed_cache  # noqa: F401
