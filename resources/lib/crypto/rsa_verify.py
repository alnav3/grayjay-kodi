# -*- coding: utf-8 -*-
"""Pure-Python RSASSA-PKCS1-v1.5 / SHA-512 signature verification.

Mirrors Grayjay's SignatureProvider (SHA512withRSA):
    verify(text, signatureB64, publicKeyB64)
where
    * text          - the raw plugin script, verified as its UTF-8 bytes
    * publicKeyB64  - base64 of the X.509 SubjectPublicKeyInfo (SPKI) DER
    * signatureB64  - base64 of the raw signature bytes

No third-party crypto is used, because the CoreELEC target has no compiler /
pip to build `cryptography`. RSA verification is just modular exponentiation
with the public key plus a constant-structure PKCS#1 v1.5 check, which is
safe to do by hand for *verification* (no secrets involved).
"""
import base64
import hashlib

# ASN.1 DigestInfo prefix for SHA-512 (RFC 8017, EMSA-PKCS1-v1_5).
_SHA512_DIGESTINFO = bytes.fromhex("3051300d060960864801650304020305000440")


# -- minimal DER reader --------------------------------------------------
def _read_tlv(data, idx):
    """Return (tag, value_bytes, next_index) for the TLV at data[idx]."""
    tag = data[idx]
    idx += 1
    length = data[idx]
    idx += 1
    if length & 0x80:
        num = length & 0x7F
        length = int.from_bytes(data[idx:idx + num], "big")
        idx += num
    value = data[idx:idx + length]
    return tag, value, idx + length


def _spki_to_rsa_numbers(spki_der):
    """Extract (n, e) from an X.509 SubjectPublicKeyInfo DER blob."""
    # outer SEQUENCE
    tag, outer, _ = _read_tlv(spki_der, 0)
    if tag != 0x30:
        raise ValueError("SPKI: expected outer SEQUENCE")
    # AlgorithmIdentifier SEQUENCE (skip), then BIT STRING
    _, _algid, idx = _read_tlv(outer, 0)
    tag, bitstring, _ = _read_tlv(outer, idx)
    if tag != 0x03:
        raise ValueError("SPKI: expected BIT STRING")
    # BIT STRING: first byte = unused bits (expected 0), rest = RSAPublicKey DER
    rsa_der = bitstring[1:]
    tag, rsa_seq, _ = _read_tlv(rsa_der, 0)
    if tag != 0x30:
        raise ValueError("SPKI: expected RSAPublicKey SEQUENCE")
    tag, n_bytes, idx = _read_tlv(rsa_seq, 0)
    if tag != 0x02:
        raise ValueError("SPKI: expected INTEGER modulus")
    tag, e_bytes, _ = _read_tlv(rsa_seq, idx)
    if tag != 0x02:
        raise ValueError("SPKI: expected INTEGER exponent")
    n = int.from_bytes(n_bytes, "big")
    e = int.from_bytes(e_bytes, "big")
    return n, e


def _i2osp(x, length):
    return x.to_bytes(length, "big")


def verify(text, signature_b64, public_key_b64):
    """Return True iff the signature is valid for `text` under the public key."""
    try:
        n, e = _spki_to_rsa_numbers(base64.b64decode(public_key_b64))
        sig = base64.b64decode(signature_b64)
        k = (n.bit_length() + 7) // 8
        if len(sig) != k:
            return False
        # RSAVP1: m = sig^e mod n
        s = int.from_bytes(sig, "big")
        if s >= n:
            return False
        m = pow(s, e, n)
        em = _i2osp(m, k)
        # Build expected EMSA-PKCS1-v1_5 encoding and compare.
        if isinstance(text, str):
            text = text.encode("utf-8")
        digest = hashlib.sha512(text).digest()
        t = _SHA512_DIGESTINFO + digest
        ps_len = k - len(t) - 3
        if ps_len < 8:
            return False
        expected = b"\x00\x01" + (b"\xff" * ps_len) + b"\x00" + t
        return _consteq(em, expected)
    except Exception:
        return False


def _consteq(a, b):
    if len(a) != len(b):
        return False
    r = 0
    for x, y in zip(a, b):
        r |= x ^ y
    return r == 0
