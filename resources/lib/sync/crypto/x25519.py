"""X25519 key exchange per RFC 7748.

Computes the X25519(k, u) function on Curve25519:
    y^2 = x^3 + 486662*x^2 + x  over GF(2^255 - 19)

Inputs are 32-byte little-endian scalars (with clamping on the scalar per
spec). Output is the u-coordinate of the resulting point, also 32-byte LE.

This is slow in pure Python but it's only used once per handshake — well
under 100 ms on the slowest target (32-bit ARM Kodi), which is fine for a
manual pairing flow.
"""
from .chacha20_poly1305 import _rotl32  # not used; placeholder to keep imports tidy


_P = (1 << 255) - 19
_A24 = 121665


def _decode_scalar(k):
    """Clamp the scalar per RFC 7748 §5: k[0] &= 248, k[31] &= 127, k[31] |= 64."""
    k = bytearray(k)
    if len(k) != 32:
        raise ValueError("scalar must be 32 bytes")
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    return int.from_bytes(bytes(k), "little")


def _decode_u(u):
    """Decode a Curve25519 u-coordinate. Mask the high bit (sign bit) per spec."""
    u = bytearray(u)
    if len(u) != 32:
        raise ValueError("u-coordinate must be 32 bytes")
    u[31] &= 127
    return int.from_bytes(bytes(u), "little")


def _encode(n):
    return n.to_bytes(32, "little")


def x25519(scalar, u):
    """X25519(scalar, u). Both inputs are 32 bytes; output is 32 bytes.

    Scalar is clamped per spec. The u-coordinate's high bit is masked (per
    spec) so a peer can't force a particular sign. Returns all-zero bytes
    on a low-order point (RFC 7748 §6.1) — callers should treat that as
    "abort the handshake", which is exactly what we want for a single-shot
    pairing flow."""
    k = _decode_scalar(scalar)
    x_1 = _decode_u(u)

    x_2, z_2 = 1, 0
    x_3, z_3 = x_1, 1
    swap = 0
    p = _P
    a24 = _A24

    # Montgomery ladder. Always 255 iterations regardless of bits set.
    for t in range(254, -1, -1):
        k_t = (k >> t) & 1
        swap ^= k_t
        if swap:
            x_2, x_3 = x_3, x_2
            z_2, z_3 = z_3, z_2
        swap = k_t

        # Differential addition: compute (x_3, z_3) = 2*(x_2, z_2) - (x_1, z_1) on the
        # Montgomery curve, given u = x_1 as a fixed input. The classic ladder form.
        A = (x_2 + z_2) % p
        AA = (A * A) % p
        B = (x_2 - z_2) % p
        BB = (B * B) % p
        E = (AA - BB) % p
        C = (x_3 + z_3) % p
        D = (x_3 - z_3) % p
        DA = (D * A) % p
        CB = (C * B) % p
        x_3 = ((DA + CB) * (DA + CB)) % p
        z_3 = (x_1 * ((DA - CB) * (DA - CB)) % p) % p
        x_2 = (AA * BB) % p
        z_2 = (E * ((AA + a24 * E) % p)) % p

    # If swap was set after the last iteration, swap back.
    if swap:
        x_2, x_3 = x_3, x_2
        z_2, z_3 = z_3, z_2

    # z_2^(-1) * x_2, taken mod p.
    res = (x_2 * pow(z_2, p - 2, p)) % p
    return _encode(res)


def generate_keypair():
    """Generate a fresh X25519 keypair. Returns (private, public) as 32-byte
    little-endian bytes. Caller is responsible for persisting the private key."""
    import os
    private = os.urandom(32)
    # Clamp before scalar mult so the public point is well-defined.
    clamped = bytearray(private)
    clamped[0] &= 248
    clamped[31] &= 127
    clamped[31] |= 64
    # Multiply by the basepoint (u=9, the canonical Curve25519 generator).
    public = x25519(bytes(clamped), b"\x09" + b"\x00" * 31)
    return bytes(private), public