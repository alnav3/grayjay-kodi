"""Noise's variant of HKDF (NOT RFC 5869).

The Noise Protocol Framework defines its own Extract+Expand: temp_key =
HMAC(ck, ikm), then output_i = HMAC(temp_key, output_(i-1) || counter_i)
where output_0 is empty and counters are 1, 2, 3, ... Used for both
MixKey (2 outputs) and Split (3 outputs).

We use BLAKE2b-512 as HASH (H_LEN = 64). HMAC is RFC 2104 over HASH.
"""
import hashlib


_H_LEN = 64  # BLAKE2b-512 output size
_BLOCK_LEN = 128  # BLAKE2b block size


def H(data):
    """HASH() in Noise notation."""
    return hashlib.blake2b(data, digest_size=_H_LEN).digest()


def _hmac(key, data1=b"", data2=b""):
    """One-shot HMAC over HASH, taking up to two data segments (Noise chains
    the previous output into the next call's input)."""
    if len(key) > _BLOCK_LEN:
        key = H(key)
    if len(key) < _BLOCK_LEN:
        key = key + b"\x00" * (_BLOCK_LEN - len(key))
    o_key_pad = bytes(b ^ 0x5C for b in key)
    i_key_pad = bytes(b ^ 0x36 for b in key)
    inner = H(i_key_pad + data1 + data2)
    return H(o_key_pad + inner)


def HKDF(ck, ikm, num_outputs):
    """Noise HKDF. Returns a tuple of num_outputs H_LEN-byte blocks."""
    if num_outputs < 1 or num_outputs > 3:
        raise ValueError("num_outputs must be 1, 2, or 3")
    temp_key = _hmac(ck, ikm)
    outputs = []
    prev = b""
    for counter in range(1, num_outputs + 1):
        out = _hmac(temp_key, prev, bytes([counter]))
        outputs.append(out)
        prev = out
    return tuple(outputs)


def HKDF2(ck, ikm):
    """MixKey helper: returns (new_ck, temp_k1, temp_k2)."""
    return HKDF(ck, ikm, 2)


def HKDF3(ck, ikm):
    """Split helper: returns (k1, k2, k3)."""
    return HKDF(ck, ikm, 3)