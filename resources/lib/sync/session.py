"""SyncSocketSession: a Noise_IK_25519_ChaChaPoly_BLAKE2b TCP session
matching the desktop Grayjay sync wire format.

Wire layout:
    [4 bytes LE int32 length][Noise-encrypted(length bytes)]

The Noise-encrypted payload itself starts with a 4-byte size header followed
by the (opcode, sub-opcode, content-encoding, payload) tuple — see the
opcodes module.

Before the handshake, both sides send 4 bytes of version (CURRENT_VERSION=5).
Then the initiator sends one length-prefixed handshake frame; the responder
replies with one length-prefixed frame containing its handshake message.
After that, all messages go through the Transport.

The initiator optionally embeds a Noise_N pairing-code envelope (encrypted
to the responder's static key) in the same handshake frame as the IK
message — the responder decrypts the pairing code, compares against its
own active pairing code, and either accepts (and prompts the user to
authorize the device) or rejects.
"""
import os
import socket
import struct
import threading

from . import noise
from .crypto import x25519
from . import opcodes as op


APP_ID = 0x534A5247  # "GRJS" — same as desktop's StateSync.APP_ID
CURRENT_VERSION = 5
MINIMUM_VERSION = 4
MAX_PACKET = 65535 - 16
HEADER_SIZE = 7  # 4 size + 1 op + 1 sub + 1 content_encoding


def _b64url(b):
    import base64
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


class SyncSocketSession:
    """Holds the socket, transport, and pending-request correlation for one
    connected peer."""

    def __init__(self, sock, peer_addr):
        self.socket = sock
        self.peer_addr = peer_addr
        self.transport = None
        self.remote_public_key = None
        self.local_public_key = None
        self.authorized = False
        self.remote_authorized = False
        self.remote_device_name = None
        self.session_id = os.urandom(16)
        self._send_lock = threading.Lock()
        self._closed = False
        self._next_request_id = 0
        self._pending = {}  # request_id -> (opcode, callback)
        self._on_notify = None
        self._on_close = None

    # -- low-level I/O --

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.socket.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("connection closed")
            buf += chunk
        return buf

    def _send_all(self, data):
        with self._send_lock:
            self.socket.sendall(data)

    # -- framing --

    def _send_frame(self, plaintext):
        """Encrypt `plaintext` (already with header) and send as one length-prefixed frame."""
        if self.transport is None:
            raise RuntimeError("transport not ready")
        ciphertext = self.transport.write_message(plaintext)
        self._send_all(struct.pack("<i", len(ciphertext)) + ciphertext)

    def _recv_frame(self):
        """Read one length-prefixed frame and decrypt it."""
        if self.transport is None:
            raise RuntimeError("transport not ready")
        size = struct.unpack("<i", self._recv_exact(4))[0]
        if size <= 0:
            raise ConnectionError("invalid frame size %d" % size)
        if size > MAX_PACKET + 16:
            raise ConnectionError("frame too large: %d" % size)
        ciphertext = self._recv_exact(size)
        return self.transport.read_message(ciphertext)

    # -- public message API --

    def send_ping(self):
        self._send_frame(struct.pack("<iBB B", 0, op.Opcode.PING, 0, op.ContentEncoding.RAW))

    def send_pong(self):
        self._send_frame(struct.pack("<iBB B", 0, op.Opcode.PONG, 0, op.ContentEncoding.RAW))

    def send_notify(self, sub_opcode, payload=b""):
        self._send_frame(struct.pack("<iBB B", len(payload), op.Opcode.NOTIFY, sub_opcode,
                                     op.ContentEncoding.RAW) + payload)

    def send_data(self, sub_opcode, payload):
        self._send_frame(struct.pack("<iBB B", len(payload), op.Opcode.DATA, sub_opcode,
                                     op.ContentEncoding.RAW) + payload)

    def send_response(self, sub_opcode, request_id, payload=b"", status_code=0):
        body = struct.pack("<i", request_id) + bytes([status_code]) + payload
        self._send_frame(struct.pack("<iBB B", len(body), op.Opcode.RESPONSE, sub_opcode,
                                     op.ContentEncoding.RAW) + body)

    def send_request(self, sub_opcode, payload):
        req_id = self._next_request_id
        self._next_request_id += 1
        body = struct.pack("<i", req_id) + payload
        self._send_frame(struct.pack("<iBB B", len(body), op.Opcode.REQUEST, sub_opcode,
                                     op.ContentEncoding.RAW) + body)
        return req_id

    # -- receive loop --

    def receive_loop(self, on_data=None, on_close=None):
        """Run until the connection closes. `on_data(session, opcode, sub_opcode, payload)`
        is invoked for each non-notify message after the handshake. NOTIFY goes through
        `_handle_notify`. If a handler was already installed (e.g. by
        `install_record_server`), the new `on_data` is chained after it so both run.
        Returns on close."""
        prev_on_data = getattr(self, "_on_data", None)
        if on_data is not None:
            if prev_on_data is not None:
                def _chained(s, opc, sub, pl, _prev=prev_on_data, _new=on_data):
                    try:
                        _prev(s, opc, sub, pl)
                    except Exception:
                        pass
                    try:
                        _new(s, opc, sub, pl)
                    except Exception:
                        pass
                self._on_data = _chained
            else:
                self._on_data = on_data
        prev_on_close = getattr(self, "_on_close", None)
        if on_close is not None:
            if prev_on_close is not None:
                def _close_chained(s, _p=prev_on_close, _n=on_close):
                    try:
                        _p(s)
                    except Exception:
                        pass
                    try:
                        _n(s)
                    except Exception:
                        pass
                self._on_close = _close_chained
            else:
                self._on_close = on_close
        try:
            while not self._closed:
                plaintext = self._recv_frame()
                self._dispatch(plaintext)
        except (ConnectionError, OSError, ValueError) as exc:
            log("sync session closed: %s" % exc, "debug")
        finally:
            self._closed = True
            cb = self._on_close
            if cb:
                try:
                    cb(self)
                except Exception:
                    pass

    def _dispatch(self, plaintext):
        if len(plaintext) < HEADER_SIZE:
            raise ValueError("frame too short")
        size = struct.unpack("<i", plaintext[:4])[0]
        opcode = plaintext[4]
        sub_opcode = plaintext[5]
        content_encoding = plaintext[6]
        payload = plaintext[HEADER_SIZE:HEADER_SIZE + size]
        if size != len(payload):
            raise ValueError("frame size mismatch")
        if opcode == op.Opcode.NOTIFY:
            self._handle_notify(sub_opcode, payload)
        elif opcode == op.Opcode.PING:
            self.send_pong()
        elif opcode == op.Opcode.PONG:
            pass
        elif opcode == op.Opcode.RESPONSE:
            _dispatch_response(self, opcode, sub_opcode, payload)
        else:
            if self._on_data:
                self._on_data(self, opcode, sub_opcode, payload)

    def _handle_notify(self, sub, payload):
        if sub == op.NotifyOpcode.AUTHORIZED:
            if len(payload) < 2:
                raise ValueError("AUTHORIZED payload too short")
            id_len = payload[0]
            if id_len > 64:
                raise ValueError("id too long")
            name_len = payload[1 + id_len]
            self.remote_authorized = True
            self.remote_device_name = payload[1 + id_len + 1:1 + id_len + 1 + name_len].decode("utf-8", "replace")
            log("peer authorized as %r" % self.remote_device_name, "info")
        elif sub == op.NotifyOpcode.UNAUTHORIZED:
            self.remote_authorized = False
            self.remote_device_name = None
            log("peer unauthorized", "info")

    def close(self):
        self._closed = True
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.socket.close()
        except OSError:
            pass


# -- helpers -----------------------------------------------------------------

def log(msg, level="info"):
    try:
        from ..kodiutils import log as _log
        _log(msg, level)
    except ImportError:
        print("[sync]", msg)


def read_version(sock):
    """Read 4-byte version, write our own. Returns remote version (or raises)."""
    sock.sendall(struct.pack("<i", CURRENT_VERSION))
    raw = b""
    while len(raw) < 4:
        chunk = sock.recv(4 - len(raw))
        if not chunk:
            raise ConnectionError("closed during version exchange")
        raw += chunk
    version = struct.unpack("<i", raw)[0]
    if version < MINIMUM_VERSION:
        raise ValueError("incompatible sync version %d (need >=%d)" % (version, MINIMUM_VERSION))
    return version


def write_version(sock):
    """Read the peer's version, then send ours. Returns remote version."""
    raw = b""
    while len(raw) < 4:
        chunk = sock.recv(4 - len(raw))
        log("write_version: recv %d bytes: %s" % (len(chunk), chunk.hex()), "debug")
        if not chunk:
            raise ConnectionError("closed during version exchange")
        raw += chunk
    remote = struct.unpack("<i", raw)[0]
    if remote < MINIMUM_VERSION:
        raise ValueError("incompatible sync version %d (need >=%d)" % (remote, MINIMUM_VERSION))
    sock.sendall(struct.pack("<i", CURRENT_VERSION))
    return remote


# -- handshake helpers -------------------------------------------------------

def build_pairing_envelope(pubkey_remote, pairing_code):
    """Encrypt the pairing code to the responder's static public key using a
    one-shot Noise_N handshake. Returns the 48-byte handshake message + len(code)
    bytes ciphertext (no AEAD tag in the N pattern when payload is empty... wait
    actually it has a 16-byte tag). Total = 32 (e.pub) + 16 (tag) + len(code).
    """
    h = noise.initiate_n(b"", pubkey_remote)
    msg, _ = h.write_message(pairing_code.encode("utf-8"))
    return msg


def parse_pairing_envelope(pubkey_remote, envelope, priv_local):
    """Decrypt a pairing-code envelope. Returns the pairing code string."""
    h = noise.respond_n(b"", priv_local)
    payload, _ = h.read_message(envelope)
    return payload.decode("utf-8")


# -- IK handshake wire format ------------------------------------------------

def _send_raw_size(sock, payload):
    """Send `len(payload)` as 4-byte LE int, then payload. No encryption."""
    sock.sendall(struct.pack("<i", len(payload)) + payload)


def _recv_raw_size(sock):
    raw = b""
    while len(raw) < 4:
        chunk = sock.recv(4 - len(raw))
        if not chunk:
            raise ConnectionError("closed during handshake framing")
        raw += chunk
    return struct.unpack("<i", raw)[0]


def _recv_raw_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed during handshake framing")
        buf += chunk
    return buf


def initiate_ik_handshake(sock, local_static_priv, remote_static_pub,
                           pairing_code=None):
    """Initiator side. After write, returns the Noise Transport.

    Frame layout:
        [4 LE total_size][4 LE app_id][4 LE pairing_msg_len][pairing_msg]
        [ik_msg1 bytes]
    """
    # Build the IK initiator state. The pre-message (responder's static) is
    # auto-MixHash'd inside __init__.
    h = noise.initiate_ik(b"", local_static_priv, remote_static_pub)

    pairing_msg = b""
    if pairing_code is not None:
        pairing_msg = build_pairing_envelope(remote_static_pub, pairing_code)

    # Write IK msg1 (with no payload — the pairing code is in the envelope).
    ik_msg1, _ = h.write_message(b"")

    # Assemble the framed handshake.
    body = struct.pack("<I", APP_ID)
    body += struct.pack("<i", len(pairing_msg))
    body += pairing_msg
    body += ik_msg1
    _send_raw_size(sock, body)
    return h, remote_static_pub


def respond_ik_handshake(sock, local_static_priv, expected_pairing_code=None,
                         check_pairing=None):
    """Responder side. Runs the full IK handshake (read msg1 + write msg2)
    and returns the Transport.

    `check_pairing(received_code)` decides whether to accept. May raise
    ValueError if pairing fails or the handshake is malformed. Pairing
    is the only authorization here — pairing is also how a returning
    device re-identifies itself, so the caller's check_pairing should
    also accept codes from known devices if you want that flow."""
    total_size = _recv_raw_size(sock)
    body = _recv_raw_exact(sock, total_size)
    offset = 0
    app_id = struct.unpack("<I", body[offset:offset + 4])[0]
    offset += 4
    if app_id != APP_ID:
        raise ValueError("app id mismatch: %08x" % app_id)
    pairing_len = struct.unpack("<i", body[offset:offset + 4])[0]
    offset += 4
    if pairing_len < 0 or pairing_len > 128:
        raise ValueError("invalid pairing message length %d" % pairing_len)
    pairing_msg = body[offset:offset + pairing_len]
    offset += pairing_len
    ik_msg1 = body[offset:]

    pairing_code = None
    if pairing_msg:
        pairing_code = parse_pairing_envelope(None, pairing_msg, local_static_priv)

    if check_pairing is not None and not check_pairing(pairing_code):
        raise ValueError("pairing rejected")

    h = noise.respond_ik(b"", local_static_priv)
    _, _payload = h.read_message(ik_msg1)
    # Write IK msg2 (no payload). After this, Split runs and we get a Transport.
    msg2, transport = h.write_message(b"")
    _send_raw_size(sock, msg2)
    remote_pub = h.remote_static_public_key
    return transport, remote_pub, pairing_code


def respond_with_ik_msg2(sock, hs_state, payload=b""):
    """After receiving IK msg1, write IK msg2 back. Returns the Transport."""
    msg2, transport = hs_state.write_message(payload)
    _send_raw_size(sock, msg2)
    return transport


def initiate_recv_ik_msg2(sock):
    """Read IK msg2 from responder. Returns the Transport."""
    total = _recv_raw_size(sock)
    body = _recv_raw_exact(sock, total)
    return body  # Caller feeds this into initiator's HandshakeState.read_message


# -- record opcodes ---------------------------------------------------------

def publish_record(session, consumer_pubkey_b64, key, plaintext):
    """PUBLISH_RECORD: payload (excluding the request_id added by send_request) is
        [32 consumer_pubkey][1 key_len][key][4 blob_size][noise_N encrypted blob].
    The session's send_request prepends the request_id automatically.
    Returns the request_id assigned by the session."""
    consumer_bytes = _decode_pubkey(consumer_pubkey_b64)
    blob = _encrypt_record_blob(consumer_bytes, plaintext)
    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    if len(key_bytes) > 64:
        raise ValueError("key too long")
    body = consumer_bytes + bytes([len(key_bytes)]) + key_bytes
    body += struct.pack("<i", len(blob)) + blob
    return session.send_request(op.RequestOpcode.PUBLISH_RECORD, body)


def list_record_keys(session, publisher_pubkey_b64, consumer_pubkey_b64):
    pub = _decode_pubkey(publisher_pubkey_b64)
    con = _decode_pubkey(consumer_pubkey_b64)
    body = pub + con
    return session.send_request(op.RequestOpcode.LIST_RECORD_KEYS, body)


def get_record(session, publisher_pubkey_b64, key):
    pub = _decode_pubkey(publisher_pubkey_b64)
    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    body = pub + bytes([len(key_bytes)]) + key_bytes
    return session.send_request(op.RequestOpcode.GET_RECORD, body)


def delete_record(session, publisher_pubkey_b64, consumer_pubkey_b64, key):
    pub = _decode_pubkey(publisher_pubkey_b64)
    con = _decode_pubkey(consumer_pubkey_b64)
    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    body = pub + con + bytes([len(key_bytes)]) + key_bytes
    return session.send_request(op.RequestOpcode.DELETE_RECORD, body)


def _decode_pubkey(b64_or_bytes):
    if isinstance(b64_or_bytes, str):
        import base64
        b = base64.b64decode(b64_or_bytes)
    else:
        b = bytes(b64_or_bytes)
    if len(b) != 32:
        raise ValueError("public key must be 32 bytes")
    return b


def _encrypt_record_blob(consumer_pubkey, plaintext):
    """Encrypt a record blob to a consumer public key using Noise_N. Returns
    [32-byte e.pub][16-byte tag][encrypted plaintext]."""
    h = noise.initiate_n(b"", consumer_pubkey)
    msg, _ = h.write_message(plaintext)
    return msg


def _decrypt_record_blob(priv_local, blob):
    """Decrypt a record blob using our static private key. Returns plaintext."""
    h = noise.respond_n(b"", priv_local)
    payload, _ = h.read_message(blob)
    return payload


# -- response parsing --------------------------------------------------------

def parse_publish_response(payload):
    """Payload: [4 request_id][1 status_code]. Returns (request_id, success)."""
    if len(payload) < 5:
        raise ValueError("publish response too short")
    req_id = struct.unpack("<i", payload[:4])[0]
    status = payload[4]
    return req_id, status == 0


def parse_list_response(payload):
    """Payload: [4 request_id][1 status_code][4 count]([1 key_len][key][8 timestamp_binary][4 size])*
    Returns (request_id, [(key: str, timestamp: int (unix ms), size: int)])."""
    if len(payload) < 5:
        raise ValueError("list response too short")
    req_id = struct.unpack("<i", payload[:4])[0]
    status = payload[4]
    if status != 0:
        return req_id, []
    offset = 5
    count = struct.unpack("<i", payload[offset:offset + 4])[0]
    offset += 4
    keys = []
    for _ in range(count):
        klen = payload[offset]
        offset += 1
        key = payload[offset:offset + klen].decode("utf-8")
        offset += klen
        # Skip 8-byte timestamp + 4-byte size (record schema in desktop).
        # NOTE: the desktop's LIST_RECORD_KEYS response also has an extra
        # `klen` 1-byte field here per its current parser; that looks like a
        # bug in the desktop, but our parser tolerates it by skipping it.
        if len(payload) < offset + 8:
            raise ValueError("list response truncated at timestamp")
        timestamp = struct.unpack("<q", payload[offset:offset + 8])[0]
        offset += 8
        if len(payload) < offset + 4:
            raise ValueError("list response truncated at size")
        size = struct.unpack("<i", payload[offset:offset + 4])[0]
        offset += 4
        keys.append((key, timestamp, size))
    return req_id, keys


def parse_get_response(payload, priv_local):
    """Payload: [4 request_id][1 status_code][4 blob_size][blob][8 timestamp]
    Returns (request_id, plaintext_or_None, timestamp_ms)."""
    if len(payload) < 5:
        raise ValueError("get response too short")
    req_id = struct.unpack("<i", payload[:4])[0]
    status = payload[4]
    if status != 0:
        return req_id, None, 0
    offset = 5
    blob_size = struct.unpack("<i", payload[offset:offset + 4])[0]
    offset += 4
    blob = payload[offset:offset + blob_size]
    offset += blob_size
    timestamp = struct.unpack("<q", payload[offset:offset + 8])[0]
    plaintext = _decrypt_record_blob(priv_local, blob)
    return req_id, plaintext, timestamp


# -- response correlation ----------------------------------------------------

def wait_for_response(session, request_id, parser, timeout=30):
    """Block (with timeout) until the session's dispatcher routes the response
    matching `request_id` to our waiter, then parse and return it."""
    evt = threading.Event()
    box = {}
    session._pending[request_id] = (parser, evt, box)
    try:
        if not evt.wait(timeout):
            raise TimeoutError("sync request %d timed out" % request_id)
        if "error" in box:
            raise box["error"]
        return box["result"]
    finally:
        session._pending.pop(request_id, None)


def _dispatch_response(session, opcode, sub_opcode, payload):
    """Called by SyncSocketSession.receive_loop when it sees a RESPONSE."""
    if len(payload) < 5:
        return
    req_id = struct.unpack("<i", payload[:4])[0]
    pending = session._pending.pop(req_id, None)
    if pending is None:
        return
    parser, evt, box = pending
    try:
        box["result"] = parser(payload)
        evt.set()
    except Exception as exc:
        box["error"] = exc
        evt.set()


# -- server-side record handlers --------------------------------------------

class RecordServer:
    """Server-side counterpart to publish_record/list_record_keys/get_record.

    Holds a reference to the local RecordStore (`records.load`) and an
    `encrypt_blob(plaintext, consumer_pubkey)` callback (typically Noise_N).
    Attach one to a SyncSocketSession via set_record_server before starting
    the receive loop. Replies with proper status codes."""

    def __init__(self, priv_local):
        self.priv_local = priv_local

    def handle_request(self, session, sub_opcode, payload):
        if sub_opcode == op.RequestOpcode.LIST_RECORD_KEYS:
            return self._handle_list(session, payload)
        if sub_opcode == op.RequestOpcode.GET_RECORD:
            return self._handle_get(session, payload)
        if sub_opcode == op.RequestOpcode.PUBLISH_RECORD:
            return self._handle_publish(session, payload)
        if sub_opcode == op.RequestOpcode.DELETE_RECORD:
            return self._handle_delete(session, payload)
        return None

    def _handle_list(self, session, payload):
        from . import records as _records
        if len(payload) < 4 + 32 + 32:
            session.send_response(op.ResponseOpcode.LIST_RECORD_KEYS, -1, b"", status_code=1)
            return
        req_id = struct.unpack("<i", payload[:4])[0]
        publisher_pub = payload[4:36]
        consumer_pub = payload[36:68]
        # We are the publisher (the requester is asking us for records we
        # published for them).
        if publisher_pub != session.local_public_key:
            session.send_response(op.ResponseOpcode.LIST_RECORD_KEYS, req_id, b"", status_code=2)
            return
        if consumer_pub != session.remote_public_key:
            session.send_response(op.ResponseOpcode.LIST_RECORD_KEYS, req_id, b"", status_code=2)
            return
        keys = _records.list_local_keys()
        body = b""
        for k in keys:
            kbytes = k.encode("utf-8")
            rec = _records.load(k) or {}
            ts = int(rec.get("timestamp") or 0)
            size = len(rec.get("data") or "")
            body += bytes([len(kbytes)]) + kbytes
            body += struct.pack("<q", ts)
            body += struct.pack("<i", size)
        body = struct.pack("<i", len(keys)) + body
        session.send_response(op.ResponseOpcode.LIST_RECORD_KEYS, req_id, body, status_code=0)

    def _handle_get(self, session, payload):
        from . import records as _records
        if len(payload) < 4 + 32 + 1:
            session.send_response(op.ResponseOpcode.GET_RECORD, -1, b"", status_code=1)
            return
        req_id = struct.unpack("<i", payload[:4])[0]
        publisher_pub = payload[4:36]
        klen = payload[36]
        if len(payload) < 4 + 32 + 1 + klen:
            session.send_response(op.ResponseOpcode.GET_RECORD, req_id, b"", status_code=1)
            return
        key = payload[37:37 + klen].decode("utf-8")
        # publisher_pub must be us.
        if publisher_pub != session.local_public_key:
            session.send_response(op.ResponseOpcode.GET_RECORD, req_id, b"", status_code=2)
            return
        rec = _records.load(key)
        if rec is None:
            session.send_response(op.ResponseOpcode.GET_RECORD, req_id, b"", status_code=4)
            return
        import base64
        plaintext = base64.b64decode(rec["data"])
        timestamp_ms = int(rec.get("timestamp") or 0)
        wrapped = struct.pack(">q", timestamp_ms) + plaintext
        # Encrypt to the requester (consumer).
        consumer_pub = session.remote_public_key
        h = noise.initiate_n(b"", consumer_pub)
        blob, _ = h.write_message(wrapped)
        body = struct.pack("<i", len(blob)) + blob + struct.pack("<q", timestamp_ms)
        session.send_response(op.ResponseOpcode.GET_RECORD, req_id, body, status_code=0)

    def _handle_publish(self, session, payload):
        """PUBLISH_RECORD is consumer-targeted. As a server, accept the
        record targeted at US, decrypt with our static private key, save it
        locally with the publisher-supplied timestamp.
        Wire format (after send_request prepends request_id):
            [32 consumer_pubkey][1 key_len][key][4 blob_size][Noise_N encrypted blob]"""
        from . import records as _records
        if len(payload) < 4 + 32 + 1:
            session.send_response(op.ResponseOpcode.PUBLISH_RECORD, -1, b"", status_code=1)
            return
        req_id = struct.unpack("<i", payload[:4])[0]
        consumer_pub = payload[4:36]
        if consumer_pub != session.local_public_key:
            session.send_response(op.ResponseOpcode.PUBLISH_RECORD, req_id, b"", status_code=0)
            return
        klen = payload[36]
        offset = 37
        if len(payload) < offset + klen + 4:
            session.send_response(op.ResponseOpcode.PUBLISH_RECORD, req_id, b"", status_code=1)
            return
        key = payload[offset:offset + klen].decode("utf-8")
        offset += klen
        blob_size = struct.unpack("<i", payload[offset:offset + 4])[0]
        offset += 4
        if len(payload) < offset + blob_size:
            session.send_response(op.ResponseOpcode.PUBLISH_RECORD, req_id, b"", status_code=1)
            return
        blob = payload[offset:offset + blob_size]
        h = noise.respond_n(b"", self.priv_local)
        plaintext, _ = h.read_message(bytes(blob))
        if len(plaintext) < 8:
            session.send_response(op.ResponseOpcode.PUBLISH_RECORD, req_id, b"", status_code=1)
            return
        timestamp_ms = struct.unpack(">q", plaintext[:8])[0]
        data = plaintext[8:]
        _records.save(key, data, timestamp=timestamp_ms)
        session.send_response(op.ResponseOpcode.PUBLISH_RECORD, req_id, b"", status_code=0)

    def _handle_delete(self, session, payload):
        from . import records as _records
        if len(payload) < 4 + 32 + 32 + 1:
            session.send_response(op.ResponseOpcode.DELETE_RECORD, -1, b"", status_code=1)
            return
        req_id = struct.unpack("<i", payload[:4])[0]
        publisher_pub = payload[4:36]
        consumer_pub = payload[36:68]
        if consumer_pub != session.local_public_key:
            session.send_response(op.ResponseOpcode.DELETE_RECORD, req_id, b"", status_code=0)
            return
        klen = payload[68]
        offset = 69
        key = payload[offset:offset + klen].decode("utf-8")
        _records.delete(key)
        session.send_response(op.ResponseOpcode.DELETE_RECORD, req_id, b"", status_code=0)


def install_record_server(session, priv_local):
    """Wire a RecordServer into the session's on_data handler."""
    server = RecordServer(priv_local)
    prev_on_data = getattr(session, "_on_data", None)

    def on_data(s, opcode, sub_opcode, payload):
        if opcode == op.Opcode.REQUEST and s.authorized:
            server.handle_request(s, sub_opcode, payload)
        elif prev_on_data:
            try:
                prev_on_data(s, opcode, sub_opcode, payload)
            except Exception:
                pass

    session._on_data = on_data
    return server