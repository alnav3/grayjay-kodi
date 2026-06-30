# -*- coding: utf-8 -*-
"""Curated list of Grayjay's official source plugins.

These are FUTO's first-party plugin configs hosted on plugins.grayjay.app. The
app ships them as its built-in sources; we offer the same set from the "Add
source" menu so users can install one with a single click instead of pasting a
URL. Each is signed, so install/update still verifies the signature.

There is no public machine-readable index of these URLs, so the list is curated
here. Every entry below was confirmed live (HTTP 200 + valid config). `name` is
the label shown in the menu, kept as the plugin reports it (including any
"(Beta)"/"(Alpha)" maturity tag). Note the folder/config casing is exact and
not always uniform (e.g. Soundcloud, BiliBili) — copy it verbatim when adding.

To refresh: probe https://plugins.grayjay.app/<Folder>/<Config>Config.json.
"""

_BASE = "https://plugins.grayjay.app"

# (name, config_url) — grouped video → regional/other → audio/podcasts.
OFFICIAL_SOURCES = [
    # Mainstream video
    ("Youtube",                 _BASE + "/Youtube/YoutubeConfig.json"),
    ("Odysee",                  _BASE + "/Odysee/OdyseeConfig.json"),
    ("Rumble",                  _BASE + "/Rumble/RumbleConfig.json"),
    ("PeerTube",                _BASE + "/PeerTube/PeerTubeConfig.json"),
    ("Nebula",                  _BASE + "/Nebula/NebulaConfig.json"),
    ("Patreon",                 _BASE + "/Patreon/PatreonConfig.json"),
    ("Twitch (Beta)",           _BASE + "/Twitch/TwitchConfig.json"),
    ("Kick (Beta)",             _BASE + "/Kick/KickConfig.json"),
    ("Dailymotion (Beta)",      _BASE + "/Dailymotion/DailymotionConfig.json"),
    ("Bitchute (Beta)",         _BASE + "/Bitchute/BitchuteConfig.json"),
    ("TikTok",                  _BASE + "/TikTok/TikTokConfig.json"),
    # Regional / niche video
    ("BiliBili (CN)",           _BASE + "/Bilibili/BiliBiliConfig.json"),
    ("Niconico",                _BASE + "/Niconico/NiconicoConfig.json"),
    ("Ted Talks (Alpha)",       _BASE + "/TedTalks/TedTalksConfig.json"),
    ("Curiosity Stream (Alpha)", _BASE + "/CuriosityStream/CuriosityStreamConfig.json"),
    ("Crunchyroll (Alpha)",     _BASE + "/Crunchyroll/CrunchyrollConfig.json"),
    ("Internet Archive (Alpha)", _BASE + "/InternetArchive/InternetArchiveConfig.json"),
    # Audio / podcasts
    ("SoundCloud",              _BASE + "/Soundcloud/SoundcloudConfig.json"),
    ("Spotify",                 _BASE + "/Spotify/SpotifyConfig.json"),
    ("Apple Podcasts",          _BASE + "/ApplePodcasts/ApplePodcastsConfig.json"),
    ("Mixcloud (Alpha)",        _BASE + "/Mixcloud/MixcloudConfig.json"),
]
