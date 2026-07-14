"""Noise_IK_25519_ChaChaPoly_BLAKE2b (and Noise_N_...) implementation.

Mirrors the C# desktop's Noise implementation byte-for-byte so the wire
format is interoperable. We only implement the two patterns the sync
protocol needs:
    - IK: a 2-message mutual-auth handshake used for the main connection.
          The initiator knows the responder's static public key upfront
          (carried in the grayjay:// pairing URL).
    - N:  a one-way handshake used per-record to encrypt a record blob to
          a single consumer public key.

Cipher = ChaCha20-Poly1305   (RFC 8439)
DH     = X25519              (RFC 7748)
Hash   = BLAKE2b-512         (hashlib)

References: Noise Protocol Framework rev 34.
"""
import os

from .crypto import chacha20_poly1305 as aead
from .crypto import x25519
from .crypto.hkdf import H as BLAKE2b, HKDF


H_LEN = 64
DH_LEN = 32
KEY_LEN = 32
TAG_LEN = 16

PROTOCOL_NAME = b"Noise_IK_25519_ChaChaPoly_BLAKE2b"
PROTOCOL_NAME_N = b"Noise_N_25519_ChaChaPoly_BLAKE2b"

_BASEPOINT = b"\x09" + b"\x00" * 31  # Curve25519 generator u-coordinate


def _priv_to_pub(priv):
    return x25519.x25519(priv, _BASEPOINT)


def _dh(priv_scalar, pub_bytes):
    return x25519.x25519(priv_scalar, pub_bytes)


# -- CipherState -------------------------------------------------------------

class CipherState:
    """AEAD state with a 32-byte key and a 64-bit nonce. None key = plaintext."""

    def __init__(self):
        self.k = None
        self.n = 0

    def has_key(self):
        return self.k is not None

    def initialize_key(self, key):
        if len(key) != KEY_LEN:
            raise ValueError("key must be 32 bytes")
        self.k = bytes(key)
        self.n = 0

    def encrypt_with_ad(self, ad, plaintext):
        if self.k is None:
            return bytes(plaintext)
        if self.n >= (1 << 64):
            raise OverflowError("nonce exhausted")
        nonce = self.n.to_bytes(12, "little")
        out = aead.encrypt(self.k, nonce, plaintext, ad or b"")
        self.n += 1
        return out

    def decrypt_with_ad(self, ad, ciphertext):
        if self.k is None:
            return bytes(ciphertext)
        if self.n >= (1 << 64):
            raise OverflowError("nonce exhausted")
        nonce = self.n.to_bytes(12, "little")
        pt = aead.decrypt(self.k, nonce, ciphertext, ad or b"")
        self.n += 1
        return pt


# -- SymmetricState ----------------------------------------------------------

class SymmetricState:
    def __init__(self, protocol_name):
        if len(protocol_name) <= H_LEN:
            self.h = bytearray(protocol_name)
        else:
            self.h = bytearray(BLAKE2b(protocol_name))
        self.ck = bytearray(self.h)
        self.state = CipherState()

    def mix_hash(self, data):
        self.h = bytearray(BLAKE2b(bytes(self.h) + bytes(data)))

    def mix_key(self, ikm):
        out1, out2 = HKDF(bytes(self.ck), bytes(ikm), 2)
        self.ck = bytearray(out1)
        self.state.initialize_key(out2[:KEY_LEN])

    def encrypt_and_hash(self, plaintext):
        ct = self.state.encrypt_with_ad(bytes(self.h), plaintext)
        self.mix_hash(ct)
        return ct

    def decrypt_and_hash(self, ciphertext):
        pt = self.state.decrypt_with_ad(bytes(self.h), ciphertext)
        self.mix_hash(ciphertext)
        return pt

    def has_key(self):
        return self.state.has_key()

    def split(self):
        out1, out2 = HKDF(bytes(self.ck), b"", 2)
        c1, c2 = CipherState(), CipherState()
        c1.initialize_key(out1[:KEY_LEN])
        c2.initialize_key(out2[:KEY_LEN])
        return c1, c2


# -- HandshakeState ----------------------------------------------------------

class HandshakeState:
    """Drives one side of a Noise handshake.

    `s` is the local static private scalar (32 bytes). `rs` is the remote
    static public key (32 bytes). For patterns with pre-messages (e.g. IK
    has the responder's static as a pre-message), the pre-message is
    MixHash'd automatically in __init__.

    After all message patterns are processed, the final call returns a
    Transport object."""

    def __init__(self, protocol_name, initiator, prologue, s, rs, patterns):
        self.protocol_name = protocol_name
        self.initiator = initiator
        self.symmetric = SymmetricState(protocol_name)
        self.symmetric.mix_hash(prologue or b"")
        self.s = s          # local static private
        self.rs = rs        # remote static public
        self.e = None       # local ephemeral (priv, pub)
        self.re = None      # remote ephemeral pub
        self.patterns = [list(p) for p in patterns]
        self.turn_to_write = initiator
        self._disposed = False
        self._pre_messages(initiator)

    @property
    def remote_static_public_key(self):
        return self.rs

    def _pre_messages(self, initiator):
        """IK pre-message: responder's static key. For the initiator we
        MixHash(rs) (the public key we were told). For the responder we
        MixHash(our own public), which is the same value as rs from the
        initiator's perspective."""
        if initiator:
            # Initiator's pre-message is empty in IK; responder's pre-message
            # is the responder's static key — which the initiator knows as rs.
            if self.rs is not None:
                self.symmetric.mix_hash(self.rs)
        else:
            # Responder's pre-message is its own static.
            if self.s is not None:
                self.symmetric.mix_hash(_priv_to_pub(self.s))

    def write_message(self, payload):
        if not self.patterns:
            raise RuntimeError("handshake already complete")
        if not self.turn_to_write:
            raise RuntimeError("expected ReadMessage, not WriteMessage")
        next_pattern = self.patterns.pop(0)
        out = bytearray()
        for token in next_pattern:
            if token == "e":
                out.extend(self._write_e())
            elif token == "s":
                out.extend(self._write_s())
            elif token == "ee":
                self._dh_and_mix_key(self.e[0], self.re)
            elif token == "es":
                if self.initiator:
                    self._dh_and_mix_key(self.e[0], self.rs)
                else:
                    self._dh_and_mix_key(self.s, self.re)
            elif token == "se":
                if self.initiator:
                    self._dh_and_mix_key(self.s, self.re)
                else:
                    self._dh_and_mix_key(self.e[0], self.rs)
            elif token == "ss":
                self._dh_and_mix_key(self.s, self.rs)
            else:
                raise ValueError("unknown token: %s" % token)
        out.extend(self.symmetric.encrypt_and_hash(bytes(payload)))
        transport = self.symmetric.split() if not self.patterns else None
        self.turn_to_write = False
        if transport is not None:
            self._disposed = True
            return bytes(out), Transport(self.initiator, *transport)
        return bytes(out), None

    def read_message(self, message):
        if not self.patterns:
            raise RuntimeError("handshake already complete")
        if self.turn_to_write:
            raise RuntimeError("expected WriteMessage, not ReadMessage")
        next_pattern = self.patterns.pop(0)
        pos = 0
        for token in next_pattern:
            if token == "e":
                pos = self._read_e(message, pos)
            elif token == "s":
                pos = self._read_s(message, pos)
            elif token == "ee":
                self._dh_and_mix_key(self.e[0], self.re)
            elif token == "es":
                if self.initiator:
                    self._dh_and_mix_key(self.e[0], self.rs)
                else:
                    self._dh_and_mix_key(self.s, self.re)
            elif token == "se":
                if self.initiator:
                    self._dh_and_mix_key(self.s, self.re)
                else:
                    self._dh_and_mix_key(self.e[0], self.rs)
            elif token == "ss":
                self._dh_and_mix_key(self.s, self.rs)
            else:
                raise ValueError("unknown token: %s" % token)
        payload = self.symmetric.decrypt_and_hash(message[pos:])
        transport = self.symmetric.split() if not self.patterns else None
        self.turn_to_write = True
        if transport is not None:
            self._disposed = True
            return payload, Transport(self.initiator, *transport)
        return payload, None

    def _write_e(self):
        priv = os.urandom(32)
        pub = _priv_to_pub(priv)
        self.e = (priv, pub)
        self.symmetric.mix_hash(pub)
        return pub

    def _read_e(self, message, pos):
        pub = bytes(message[pos:pos + DH_LEN])
        pos += DH_LEN
        self.re = pub
        self.symmetric.mix_hash(pub)
        return pos

    def _write_s(self):
        if self.s is None:
            raise RuntimeError("no local static key to send")
        pub = _priv_to_pub(self.s)
        return self.symmetric.encrypt_and_hash(pub)

    def _read_s(self, message, pos):
        length = DH_LEN + (TAG_LEN if self.symmetric.has_key() else 0)
        ct = bytes(message[pos:pos + length])
        pos += length
        pub = self.symmetric.decrypt_and_hash(ct)
        if len(pub) != DH_LEN:
            raise ValueError("decrypted static key has wrong length")
        self.rs = bytes(pub)
        return pos

    def _dh_and_mix_key(self, priv_scalar, remote_pub):
        if priv_scalar is None or remote_pub is None:
            raise RuntimeError("missing DH operand")
        shared = _dh(priv_scalar, remote_pub)
        self.symmetric.mix_key(shared)


# -- Transport ---------------------------------------------------------------

class Transport:
    """Bidirectional AEAD transport. c1 = initiator->responder, c2 = responder->initiator."""

    def __init__(self, initiator, c1, c2):
        self.initiator = initiator
        self.c1 = c1
        self.c2 = c2

    def write_message(self, payload):
        if self.initiator:
            return self.c1.encrypt_with_ad(b"", payload)
        return self.c2.encrypt_with_ad(b"", payload)

    def read_message(self, message):
        if self.initiator:
            return self.c2.decrypt_with_ad(b"", message)
        return self.c1.decrypt_with_ad(b"", message)


# -- Pattern definitions -----------------------------------------------------

# IK pattern: <- s; -> e,es,s,ss; <- e,ee,se
PATTERN_IK = [
    ["e", "es", "s", "ss"],
    ["e", "ee", "se"],
]

# N pattern: <- s; -> e,es
PATTERN_N = [
    ["e", "es"],
]


def initiate_ik(prologue, local_static_priv, remote_static_pub):
    return HandshakeState(PROTOCOL_NAME, initiator=True, prologue=prologue,
                          s=local_static_priv, rs=remote_static_pub,
                          patterns=list(PATTERN_IK))


def respond_ik(prologue, local_static_priv):
    return HandshakeState(PROTOCOL_NAME, initiator=False, prologue=prologue,
                          s=local_static_priv, rs=None,
                          patterns=list(PATTERN_IK))


def initiate_n(prologue, remote_static_pub):
    """One-way Noise_N sender. Used to encrypt a single record blob to a
    consumer public key."""
    return HandshakeState(PROTOCOL_NAME_N, initiator=True, prologue=prologue,
                          s=None, rs=remote_static_pub,
                          patterns=list(PATTERN_N))


def respond_n(prologue, local_static_priv):
    return HandshakeState(PROTOCOL_NAME_N, initiator=False, prologue=prologue,
                          s=local_static_priv, rs=None,
                          patterns=list(PATTERN_N))