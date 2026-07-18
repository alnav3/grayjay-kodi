"""Integration test for SyncService._handle_incoming auth decisions.

Verifies the three authorization branches:
  (a) Already in the authorized list -> auto-allow
  (b) Correct pairing code presented -> auto-allow (was: required a
      click-yes xbmcgui dialog, which from a worker thread always
      returned False, blocking every fresh pairing with "session not
      authorized" on Android).
  (c) Wrong / missing pairing code -> reject with empty reply.

Run with:
  PYTHONPATH=. python3 -m unittest tests.test_sync_auth
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from resources.lib.sync.crypto import x25519
from resources.lib.sync import session as sync_session
from resources.lib.sync import service as sync_service
from resources.lib import kodiutils


# Stub kodiutils so SyncService can run without Kodi (no xbmc, no dialogs).
class _StubKodiUtils:
    _profile = None
    _settings = {}

    def profile_path(self):
        return self._profile

    def get_setting(self, key, default=None):
        v = self._settings.get(key, default)
        return v

    def set_setting(self, key, value):
        self._settings[key] = str(value)

    def log(self, *args, **kwargs):
        pass

    def notify(self, *args, **kwargs):
        # Notifications don't block; swallow silently.
        pass


def _install_stub(profile_dir):
    kodiutils.profile_path = lambda: profile_dir
    kodiutils.get_setting = lambda k, d=None: _StubKodiUtils._settings.get(k, d)
    kodiutils.set_setting = lambda k, v: _StubKodiUtils._settings.update({k: str(v)})
    kodiutils.log = lambda *a, **kw: None
    kodiutils.notify = lambda *a, **kw: None
    kodiutils._HAS_KODI = False
    # Re-import the service module-level bindings now that kodiutils has stubs.
    sync_service.profile_path = kodiutils.profile_path
    sync_service.get_setting = kodiutils.get_setting
    sync_service.set_setting = kodiutils.set_setting
    sync_service.log = kodiutils.log
    sync_service.notify = kodiutils.notify


def _read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def _client_pair(kodi_pub, kodi_priv, code):
    """Initiator side: connect to kodi, send version, run IK initiator
    using a real loopback socket. Returns (a_sock, a_transport) on success,
    or (a_sock, None) if Kodi rejected."""
    a_sock = socket.create_connection(("127.0.0.1", _PORT.value), timeout=5)
    try:
        sync_session.read_version(a_sock)
        a_hs, _ = sync_session.initiate_ik_handshake(
            a_sock, kodi_priv, kodi_pub, pairing_code=code)
        msg2 = sync_session.initiate_recv_ik_msg2(a_sock)
        _, a_transport = a_hs.read_message(msg2)
        return a_sock, a_transport
    except Exception:
        try:
            a_sock.close()
        except Exception:
            pass
        return None, None


_PORT = type("Port", (), {"value": 0})()


class SyncAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = tempfile.mkdtemp()
        _install_stub(cls.profile)

    def setUp(self):
        # Each test gets a fresh service + fresh keys.
        def _rm_recursive(p):
            if os.path.isdir(p):
                for entry in os.listdir(p):
                    _rm_recursive(os.path.join(p, entry))
                try:
                    os.rmdir(p)
                except OSError:
                    pass
            else:
                try:
                    os.unlink(p)
                except OSError:
                    pass

        if os.path.isdir(self.profile):
            for entry in os.listdir(self.profile):
                _rm_recursive(os.path.join(self.profile, entry))
        _StubKodiUtils._settings = {}
        self.svc = sync_service.SyncService()
        self.svc._ensure_prepared()
        self._bind_listener()

    def _bind_listener(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(8)
        s.settimeout(1.0)
        _PORT.value = s.getsockname()[1]
        self.svc.listener_sock = s
        self.svc._listener_active = True

        def loop():
            while self.svc._listener_active:
                try:
                    sock, addr = self.svc.listener_sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(
                    target=self.svc._accept_connection, args=(sock, addr),
                    daemon=True).start()

        threading.Thread(target=loop, daemon=True).start()
        self._listener_socket = s

    def tearDown(self):
        try:
            self.svc.stop()
        except Exception:
            pass
        try:
            self._listener_socket.close()
        except Exception:
            pass

    # -- helpers ------------------------------------------------------------

    def _client_with_code(self, code):
        a_priv, a_pub = x25519.generate_keypair()
        s, transport = _client_pair(self.svc.local_pub, a_priv, code)
        return s, transport

    def _wait_for_session(self, pub_b64, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pub_b64 in self.svc._sessions:
                return self.svc._sessions[pub_b64]
            time.sleep(0.05)
        return None

    def _wait_for_some_session(self, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.svc._sessions:
                return next(iter(self.svc._sessions.values()))
            time.sleep(0.05)
        return None

    # -- tests --------------------------------------------------------------

    def test_pairing_code_alone_grants_authorization(self):
        # This is the regression: prior to the fix, a fresh pairing would
        # always be rejected because the post-handshake yesno() dialog
        # couldn't show from a worker thread.
        a_sock, a_transport = self._client_with_code(self.svc.pairing_code)
        self.assertIsNotNone(a_sock, "client rejected")
        self.assertIsNotNone(a_transport)
        try:
            # Wait for the server-side handler to register the session.
            b_sess = self._wait_for_some_session(timeout=3.0)
            self.assertIsNotNone(
                b_sess,
                "no session was registered on server side within 3s")
            # And the device should be persisted.
            with open(os.path.join(self.profile, "sync",
                                    "authorized_devices.json")) as fh:
                devs = json.load(fh)
            self.assertEqual(len(devs), 1)
            # Authoritative proof: the responder's transport can decrypt
            # what the initiator encrypted (uses the shared Noise key).
            ct = a_transport.write_message(b"hello-kodi")
            try:
                self.assertEqual(b_sess.transport.read_message(ct), b"hello-kodi")
            except Exception as exc:
                self.fail("responder could not decrypt client's payload: %s"
                          % exc)
        finally:
            a_sock.close()

    def test_already_authorized_peer_is_auto_accepted(self):
        # Device present in the authorized list — even with the OLD code
        # (which would have surfaced the click-yes prompt) the new flow
        # auto-accepts without any UI prompt.
        a_priv, a_pub = x25519.generate_keypair()
        import base64
        pub_b64 = base64.b64encode(a_pub).decode("ascii")
        devs = {pub_b64: {"name": "Test", "last_address": "127.0.0.1",
                          "added_at": int(time.time())}}
        sync_dir = os.path.join(self.profile, "sync")
        os.makedirs(sync_dir, exist_ok=True)
        with open(os.path.join(sync_dir, "authorized_devices.json"), "w") as fh:
            json.dump(devs, fh)
        a_sock, a_transport = _client_pair(
            self.svc.local_pub, a_priv, code=self.svc.pairing_code)
        self.assertIsNotNone(a_sock)
        self.assertIsNotNone(a_transport)
        a_sock.close()

    def test_wrong_pairing_code_is_rejected(self):
        a_priv, a_pub = x25519.generate_keypair()
        a_sock, a_transport = _client_pair(self.svc.local_pub, a_priv, code="WRONG-CODE")
        self.assertIsNone(a_sock, "wrong code should not have connected")

    def _find_session_by_transport(self, expected):
        for b64, sess in self.svc._sessions.items():
            if sess.transport is expected:
                return b64
        return None


if __name__ == "__main__":
    unittest.main()
