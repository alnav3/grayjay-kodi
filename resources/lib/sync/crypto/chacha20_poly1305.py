"""ChaCha20-Poly1305 AEAD per RFC 8439.

Produces byte-identical output to libsodium's crypto_aead_chacha20poly1305_ietf_*
and .NET's System.Security.Cryptography.ChaCha20Poly1305 (which the desktop
sync server uses through libsodium P/Invoke).

API:
    encrypt(key: bytes(32), nonce: bytes(12), plaintext: bytes, aad: bytes = b"")
        -> ciphertext + 16-byte tag
    decrypt(key: bytes(32), nonce: bytes(12), ciphertext_and_tag: bytes, aad: bytes = b"")
        -> plaintext (raises ValueError if tag check fails)

Ciphertext is the same length as plaintext; the 16-byte tag is appended.
"""
import struct


_MASK32 = 0xFFFFFFFF


def _rotl32(x, n):
    return ((x << n) & _MASK32) | (x >> (32 - n))


def _quarter_round(state, a, b, c, d):
    state[a] = (state[a] + state[b]) & _MASK32; state[d] ^= state[a]; state[d] = _rotl32(state[d], 16)
    state[c] = (state[c] + state[d]) & _MASK32; state[b] ^= state[c]; state[b] = _rotl32(state[b], 12)
    state[a] = (state[a] + state[b]) & _MASK32; state[d] ^= state[a]; state[d] = _rotl32(state[d], 8)
    state[c] = (state[c] + state[d]) & _MASK32; state[b] ^= state[c]; state[b] = _rotl32(state[b], 7)


def _chacha20_block(key, counter, nonce):
    """Return 64 bytes of ChaCha20 keystream for (key, counter, nonce).

    State layout (little-endian 32-bit words): constants(4) || key(8) || counter(1) || nonce(3).
    That's the IETF layout from RFC 8439 §2.3, where the 12-byte nonce occupies
    the top three words and the block counter is a single 32-bit word.
    The constants spell "expand 32-byte k" in little-endian.
    """
    if len(nonce) != 12:
        raise ValueError("IETF ChaCha20 nonce must be 12 bytes")
    state = list(struct.unpack("<4I", b"expand 32-byte k"))
    state += list(struct.unpack("<8I", key))
    state.append(counter & _MASK32)
    state += list(struct.unpack("<3I", nonce))
    working = list(state)
    for _ in range(10):
        _quarter_round(working, 0, 4, 8, 12)
        _quarter_round(working, 1, 5, 9, 13)
        _quarter_round(working, 2, 6, 10, 14)
        _quarter_round(working, 3, 7, 11, 15)
        _quarter_round(working, 0, 5, 10, 15)
        _quarter_round(working, 1, 6, 11, 12)
        _quarter_round(working, 2, 7, 8, 13)
        _quarter_round(working, 3, 4, 9, 14)
    out = bytearray(64)
    for i in range(16):
        struct.pack_into("<I", out, i * 4, (working[i] + state[i]) & _MASK32)
    return bytes(out)


def chacha20_encrypt(key, counter, nonce, data):
    """Raw ChaCha20 stream cipher. `counter` is a 32-bit unsigned int; `nonce`
    is 12 bytes (the IETF nonce layout puts counter in the low 32 bits of the
    block counter word, with a separate 64-bit block counter above it)."""
    out = bytearray(len(data))
    pos = 0
    while pos < len(data):
        block = _chacha20_block(key, counter, nonce)
        n = min(64, len(data) - pos)
        for i in range(n):
            out[pos + i] = data[pos + i] ^ block[i]
        counter = (counter + 1) & _MASK32
        pos += n
    return bytes(out)


def poly1305_mac(key, data):
    """Poly1305 one-time authenticator (RFC 8439 §2.5). Returns 16 bytes."""
    r = int.from_bytes(key[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(key[16:32], "little")
    acc = 0
    pos = 0
    while pos < len(data):
        chunk = data[pos:pos + 16]
        n = len(chunk)
        n_acc = int.from_bytes(chunk + b"\x01" * (16 - n), "little")
        acc = ((acc + n_acc) * (r + (1 << 128))) % (1 << 129)
        pos += n
    acc = (acc + s) & ((1 << 128) - 1)
    return acc.to_bytes(16, "little")


def _pad16(n):
    return (16 - (n % 16)) % 16


def encrypt(key, nonce, plaintext, aad=b""):
    """AEAD_CHACHA20_POLY1305 (RFC 8439 §2.8). Returns ciphertext || tag."""
    if len(key) != 32:
        raise ValueError("key must be 32 bytes")
    if len(nonce) != 12:
        raise ValueError("nonce must be 12 bytes")
    poly_key = chacha20_encrypt(key, 0, nonce, b"\x00" * 32)
    ciphertext = chacha20_encrypt(key, 1, nonce, plaintext)
    mac_data = aad + b"\x00" * _pad16(len(aad))
    mac_data += ciphertext + b"\x00" * _pad16(len(ciphertext))
    mac_data += struct.pack("<QQ", len(aad), len(ciphertext))
    tag = poly1305_mac(poly_key, mac_data)
    return ciphertext + tag


def decrypt(key, nonce, ciphertext_and_tag, aad=b""):
    """Inverse of encrypt(). Raises ValueError on tag mismatch."""
    if len(key) != 32:
        raise ValueError("key must be 32 bytes")
    if len(nonce) != 12:
        raise ValueError("nonce must be 12 bytes")
    if len(ciphertext_and_tag) < 16:
        raise ValueError("ciphertext too short for tag")
    ciphertext = ciphertext_and_tag[:-16]
    tag = ciphertext_and_tag[-16:]
    poly_key = chacha20_encrypt(key, 0, nonce, b"\x00" * 32)
    mac_data = aad + b"\x00" * _pad16(len(aad))
    mac_data += ciphertext + b"\x00" * _pad16(len(ciphertext))
    mac_data += struct.pack("<QQ", len(aad), len(ciphertext))
    expected = poly1305_mac(poly_key, mac_data)
    # Constant-time tag comparison.
    diff = 0
    for a, b in zip(expected, tag):
        diff |= a ^ b
    if diff != 0:
        raise ValueError("Poly1305 tag mismatch")
    return chacha20_encrypt(key, 1, nonce, ciphertext)