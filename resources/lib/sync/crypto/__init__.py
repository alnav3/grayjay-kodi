# Vendored crypto primitives for the Grayjay Noise-based sync transport.
#
# The desktop sync server uses Noise_IK_25519_ChaChaPoly_BLAKE2b, which needs
# X25519 + ChaCha20-Poly1305 + HKDF over BLAKE2b. Kodi's embedded Python
# doesn't ship `cryptography`, and a 32-bit ARM box can't pip-install it
# reliably, so we vendor small pure-Python implementations here. They match
# the relevant RFCs byte-for-byte and stay under 400 lines total, so the wire
# is identical to the desktop client.