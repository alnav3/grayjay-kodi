# -*- coding: utf-8 -*-
"""SyncService: long-running coordinator for the LAN-only sync transport.

Responsibilities:
    - Owns the local X25519 static keypair (generated on first run,
      persisted under <profile>/sync/keypair.json).
    - Owns the TCP listener socket (binds 0.0.0.0:<port>).
    - Issues a fresh 8-char pairing code on each listener start.
    - On an incoming connection: version check -> IK handshake -> if a
      pairing code is presented, validate it; otherwise check the peer's
      static key against the authorized-devices list. When authorized,
      fire `on_authorized(session)` so the caller can run the sync.
    - Maintains the authorized-devices list (public_key -> {name, last_address,
      added_at}). Persisted to <profile>/sync/authorized_devices.json.
    - Issues sync commands on demand (push our records, pull theirs).

This is intentionally narrower than the desktop's SyncService:
    - no relay, no mDNS discovery, no broadcast
    - no upstream backup/state-exchange opcodes
    - one pairing attempt at a time; if the user rejects, the connection is
      closed and we forget about that device for this session.

Wire format is identical to the desktop, so a Kodi box and a desktop app
paired with the same code will interoperate (provided the desktop is also
talking LAN-direct, which it can do via its `LocalConnections` flag).
"""
import base64
import json
import os
import socket
import struct
import threading
import time
import traceback

from . import session as sync_session
from .crypto import x25519
from ..kodiutils import profile_path, get_setting, set_setting, log


# Default port. The desktop uses 12315 too (StateSync.cs:263 PORT = 12315).
DEFAULT_PORT = 12315


class SyncService:
    """Singleton-ish service object. service.py holds one instance per
    Kodi session and calls start()/stop()."""

    def __init__(self):
        self.local_priv = None
        self.local_pub = None
        self.pairing_code = None
        self.listener_sock = None
        self.listener_thread = None
        self._running = False
        self._lock = threading.Lock()
        self._authorized_callbacks = []  # list of (session) -> None
        self._sessions = {}  # remote_pub_b64 -> SyncSocketSession

    # -- keypair & state persistence --------------------------------------

    def _keypair_path(self):
        return os.path.join(self._sync_dir(), "keypair.json")

    def _auth_path(self):
        return os.path.join(self._sync_dir(), "authorized_devices.json")

    def _sync_dir(self):
        d = os.path.join(profile_path(), "sync")
        if not os.path.isdir(d):
            os.makedirs(d)
        return d

    def _load_or_create_keypair(self):
        p = self._keypair_path()
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    kp = json.load(fh)
                priv = base64.b64decode(kp["private_key"])
                pub = base64.b64decode(kp["public_key"])
                if len(priv) == 32 and len(pub) == 32:
                    return priv, pub
                log("sync keypair file corrupt; regenerating", "warning")
            except Exception as exc:
                log("sync keypair load failed: %s" % exc, "warning")
        priv, pub = x25519.generate_keypair()
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({
                "private_key": base64.b64encode(priv).decode("ascii"),
                "public_key": base64.b64encode(pub).decode("ascii"),
            }, fh)
        log("generated sync keypair (pk=%s...)" % base64.b64encode(pub).decode("ascii")[:8], "info")
        return priv, pub

    def _load_authorized(self):
        p = self._auth_path()
        if not os.path.isfile(p):
            return {}
        try:
            with open(p, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:
            log("authorized devices load failed: %s" % exc, "warning")
            return {}

    def _save_authorized(self, devs):
        p = self._auth_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(devs, fh, indent=2, sort_keys=True)
        os.replace(tmp, p)

    # -- public API --------------------------------------------------------

    @property
    def public_key_b64(self):
        if self.local_pub is None:
            return None
        return base64.b64encode(self.local_pub).decode("ascii")

    def get_pairing_url(self):
        """Build the grayjay:// pairing URL for this device."""
        if self.local_pub is None or self.pairing_code is None:
            return None
        addrs = _local_ips()
        port = self.listener_port
        info = {
            "public_key": self.public_key_b64,
            "addresses": addrs,
            "port": port,
            "pairing_code": self.pairing_code,
        }
        body = json.dumps(info, separators=(",", ":")).encode("utf-8")
        return "grayjay://sync/" + base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")

    def parse_pairing_url(self, url):
        """Inverse of get_pairing_url. Returns dict or None."""
        if not url or not url.startswith("grayjay://sync/"):
            return None
        try:
            raw = base64.urlsafe_b64decode(url[len("grayjay://sync/"):] + "==")
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            log("parse_pairing_url failed: %s" % exc, "warning")
            return None

    def start(self):
        """Idempotent: starts the keypair, pairing code, and listener."""
        with self._lock:
            if self._running:
                return
            self.local_priv, self.local_pub = self._load_or_create_keypair()
            self.pairing_code = _generate_pairing_code()
            port = self.listener_port
            try:
                self.listener_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.listener_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.listener_sock.bind(("0.0.0.0", port))
                self.listener_sock.listen(8)
                self.listener_sock.settimeout(1.0)
            except OSError as exc:
                log("sync listener bind failed on port %d: %s" % (port, exc), "error")
                self.listener_sock = None
                return
            self._running = True
            self.listener_thread = threading.Thread(
                target=self._listener_loop, name="sync-listener", daemon=True)
            self.listener_thread.start()
            log("sync listener on :%d, pairing code %s"
                % (port, self.pairing_code), "info")

    def stop(self):
        with self._lock:
            if not self._running:
                return
            self._running = False
            try:
                if self.listener_sock:
                    self.listener_sock.close()
            except OSError:
                pass
            self.listener_sock = None
            for s in list(self._sessions.values()):
                try:
                    s.close()
                except Exception:
                    pass
            self._sessions.clear()

    @property
    def listener_port(self):
        try:
            return int(get_setting("sync_port", str(DEFAULT_PORT)))
        except (TypeError, ValueError):
            return DEFAULT_PORT

    @property
    def device_name(self):
        return get_setting("sync_device_name", "Kodi") or "Kodi"

    @property
    def is_running(self):
        return self._running

    # -- listener / acceptor -----------------------------------------------

    def _listener_loop(self):
        while self._running:
            try:
                sock, addr = self.listener_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=self._accept_connection,
                                 args=(sock, addr), daemon=True)
            t.start()

    def _accept_connection(self, sock, addr):
        try:
            remote = sync_session.write_version(sock)
            log("sync connect from %s (version %d)" % (addr, remote), "info")
            self._handle_incoming(sock, addr)
        except Exception as exc:
            log("sync accept failed: %s" % exc, "debug")
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _handle_incoming(self, sock, addr):
        """Run the responder-side IK handshake, then dispatch to authorized or
        the user-prompt path."""
        authorized = self._load_authorized()
        is_allowed = False
        remote_pub = None

        def check_pairing(received_pairing_code):
            nonlocal is_allowed, remote_pub
            if not received_pairing_code:
                return False
            if received_pairing_code != self.pairing_code:
                return False
            return True

        try:
            transport, remote_pub, received_pairing = (
                sync_session.respond_ik_handshake(
                    sock, self.local_priv,
                    expected_pairing_code=self.pairing_code,
                    check_pairing=check_pairing))
        except ValueError as exc:
            log("sync handshake rejected: %s" % exc, "info")
            return

        remote_pub_b64 = base64.b64encode(remote_pub).decode("ascii")

        # Already authorized?
        if remote_pub_b64 in authorized:
            is_allowed = True
        else:
            # Pairing code matched; prompt user via Kodi dialog (in a worker
            # thread so the listener loop isn't blocked).
            decision = {"ok": False}

            def _prompt():
                try:
                    import xbmcgui
                    label = (received_pairing or "")
                    ok = xbmcgui.Dialog().yesno(
                        "Authorize sync device",
                        "A new device wants to sync with this Kodi.\n\n"
                        "Public key:\n%s...\n\n"
                        "Pairing code: %s\n\n"
                        "Authorize?"
                        % (remote_pub_b64[:16], label))
                    decision["ok"] = bool(ok)
                except Exception as exc:
                    log("auth prompt failed: %s" % exc, "error")

            t = threading.Thread(target=_prompt, daemon=True)
            t.start()
            t.join(timeout=120)
            is_allowed = decision["ok"]

        if not is_allowed:
            try:
                sock.sendall(struct.pack("<i", 0))  # empty handshake reply = reject
            except OSError:
                pass
            return

        # Add (or refresh) authorized entry.
        authorized[remote_pub_b64] = {
            "name": "(pending)",
            "last_address": addr[0],
            "added_at": int(time.time()),
        }
        self._save_authorized(authorized)

        # The IK msg2 was already written by respond_ik_handshake. Attach
        # the Transport to a session and proceed.
        sess = sync_session.SyncSocketSession(sock, "%s:%d" % (addr[0], addr[1]))
        sess.local_public_key = self.local_pub
        sess.remote_public_key = remote_pub
        sess.transport = transport

        # Mark authorized + send AUTHORIZED notify.
        self._sessions[remote_pub_b64] = sess
        id_str = sess.session_id.hex()
        id_bytes = id_str.encode("utf-8")
        name_bytes = self.device_name.encode("utf-8")
        payload = bytes([len(id_bytes)]) + id_bytes + bytes([len(name_bytes)]) + name_bytes
        sess.send_notify(0, payload)  # NotifyOpcode.AUTHORIZED = 0
        sess.authorized = True

        # Update name from AUTHORIZED reply.
        def on_data(s, opc, sub, pl):
            pass  # reserved for sync data

        def on_close(s):
            self._sessions.pop(remote_pub_b64, None)

        threading.Thread(target=sess.receive_loop,
                         args=(on_data, on_close), daemon=True).start()

        for cb in list(self._authorized_callbacks):
            try:
                cb(sess)
            except Exception as exc:
                log("authorized callback failed: %s" % exc, "error")

    # -- outbound connect --------------------------------------------------

    def connect_and_pair(self, info, post_auth_callback=None):
        """Connect to a peer using a parsed pairing URL. `info` is the dict
        returned by parse_pairing_url(). Triggers pairing flow (no active
        pairing code required — we supply the one from the URL)."""
        pub_b64 = info.get("public_key")
        if not pub_b64:
            raise ValueError("pairing URL missing public_key")
        try:
            pub = base64.b64decode(pub_b64)
        except Exception:
            raise ValueError("invalid public_key in pairing URL")
        addrs = info.get("addresses") or []
        port = int(info.get("port") or DEFAULT_PORT)
        code = info.get("pairing_code") or ""
        if not addrs:
            raise ValueError("pairing URL has no addresses")
        last_err = None
        for addr in addrs:
            try:
                sock = socket.create_connection((addr, port), timeout=10)
                break
            except OSError as exc:
                last_err = exc
                continue
        else:
            raise RuntimeError("could not connect to any address: %s" % last_err)

        try:
            remote_v = sync_session.read_version(sock)
            log("sync connect to %s:%d (version %d)" % (addr, port, remote_v), "info")
            hs, _ = sync_session.initiate_ik_handshake(sock, self.local_priv, pub, pairing_code=code)
            msg2 = sync_session.initiate_recv_ik_msg2(sock)
            _, transport = hs.read_message(msg2)
        except Exception as exc:
            log("sync handshake as initiator failed: %s" % exc, "error")
            sock.close()
            raise

        sess = sync_session.SyncSocketSession(sock, "%s:%d" % (addr, port))
        sess.local_public_key = self.local_pub
        sess.remote_public_key = pub
        sess.transport = transport
        sess.authorized = True  # we initiated, so we're the authorized side
        remote_pub_b64 = pub_b64

        # Wait briefly for AUTHORIZED reply, then proceed.
        def on_data(s, opc, sub, pl):
            pass

        def on_close(s):
            self._sessions.pop(remote_pub_b64, None)

        threading.Thread(target=sess.receive_loop,
                         args=(on_data, on_close), daemon=True).start()

        # Persist this as an authorized device.
        auth = self._load_authorized()
        auth[remote_pub_b64] = {
            "name": "(pending)",
            "last_address": addr,
            "added_at": int(time.time()),
        }
        self._save_authorized(auth)
        self._sessions[remote_pub_b64] = sess

        if post_auth_callback:
            try:
                post_auth_callback(sess)
            except Exception as exc:
                log("post-auth callback failed: %s" % exc, "error")

    # -- authorized device management --------------------------------------

    def list_authorized(self):
        return self._load_authorized()

    def remove_authorized(self, public_key_b64):
        auth = self._load_authorized()
        if public_key_b64 in auth:
            del auth[public_key_b64]
            self._save_authorized(auth)
            sess = self._sessions.pop(public_key_b64, None)
            if sess:
                sess.close()

    def rename_device(self, public_key_b64, name):
        auth = self._load_authorized()
        if public_key_b64 in auth:
            auth[public_key_b64]["name"] = name
            self._save_authorized(auth)

    def on_authorized(self, callback):
        """Register a callback fired when a new device is authorized (incoming
        or via pairing URL). Called with the SyncSocketSession."""
        self._authorized_callbacks.append(callback)


# -- helpers -----------------------------------------------------------------

def _generate_pairing_code():
    """8-char ambiguous-free pairing code, matching desktop's alphabet
    (no 0/O, no 1/I)."""
    import secrets
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


def _local_ips():
    """Return a list of non-loopback IPv4 addresses for this host. Best
    effort — falls back to 127.0.0.1 on failure."""
    addrs = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                addrs.append(ip)
    except OSError:
        pass
    if not addrs:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                addrs.append(s.getsockname()[0])
            finally:
                s.close()
        except OSError:
            pass
    if not addrs:
        addrs = ["127.0.0.1"]
    return list(dict.fromkeys(addrs))  # de-dup preserving order