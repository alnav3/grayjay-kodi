"""Smoke test: two SyncService-like objects connect over TCP, complete the
IK handshake with a pairing code, and exchange a Noise-encrypted ping/pong."""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from resources.lib.sync.crypto import x25519
from resources.lib.sync import session as sync_session
from resources.lib.sync.service import _generate_pairing_code


def run():
    # Two endpoints with their own "profiles".
    a_profile = tempfile.mkdtemp()
    b_profile = tempfile.mkdtemp()
    os.environ["GRAYJAY_PROFILE_OVERRIDE"] = a_profile

    # Stub kodiutils so we don't need Kodi.
    from resources.lib import kodiutils
    kodiutils.profile_path = lambda: os.environ.get("GRAYJAY_PROFILE_OVERRIDE", a_profile)

    a_priv, a_pub = x25519.generate_keypair()
    b_priv, b_pub = x25519.generate_keypair()
    pairing = _generate_pairing_code()
    print("pairing:", pairing)

    # Listen on b.
    import socket
    b_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    b_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    b_sock.bind(("127.0.0.1", 0))
    b_sock.listen(1)
    b_port = b_sock.getsockname()[1]

    accepted = {}

    def b_accept():
        sock, addr = b_sock.accept()
        try:
            sync_session.write_version(sock)
            def check_pairing(code):
                return code == pairing
            transport, remote_pub, received_code = sync_session.respond_ik_handshake(
                sock, b_priv, expected_pairing_code=pairing, check_pairing=check_pairing)
            accepted["remote_pub"] = remote_pub
            accepted["received_code"] = received_code
            accepted["transport"] = transport
            accepted["sock"] = sock
        except Exception as exc:
            print("b_accept failed:", exc)
            sock.close()

    threading.Thread(target=b_accept, daemon=True).start()

    # a connects
    a_sock = socket.create_connection(("127.0.0.1", b_port), timeout=5)
    try:
        sync_session.read_version(a_sock)
        a_hs, _ = sync_session.initiate_ik_handshake(a_sock, a_priv, b_pub, pairing_code=pairing)
        msg2 = sync_session.initiate_recv_ik_msg2(a_sock)
        _, a_transport = a_hs.read_message(msg2)
        print("a got transport:", a_transport is not None)

        # Wait for b_accept to finish
        for _ in range(50):
            if "transport" in accepted:
                break
            time.sleep(0.05)
        b_transport = accepted["transport"]
        b_sock_conn = accepted["sock"]
        print("b got transport:", b_transport is not None)
        print("b received code:", accepted["received_code"])

        # Now exchange encrypted pings/pongs.
        ct = a_transport.write_message(b"ping from a")
        pt = b_transport.read_message(ct)
        print("a -> b:", pt)
        assert pt == b"ping from a"

        ct2 = b_transport.write_message(b"pong from b")
        pt2 = a_transport.read_message(ct2)
        print("b -> a:", pt2)
        assert pt2 == b"pong from b"

        # Confirm we derive the same secrets.
        assert accepted["remote_pub"] == a_pub, "remote pub mismatch"

        # Confirm the Noise_N envelope (record blob) round-trips too.
        envelope = sync_session.build_pairing_envelope(a_pub, "test-pairing-code")
        decoded = sync_session.parse_pairing_envelope(a_pub, envelope, a_priv)
        print("envelope round-trip:", decoded)

        print("ALL OK")
    finally:
        a_sock.close()
        if "sock" in accepted:
            accepted["sock"].close()
        b_sock.close()


if __name__ == "__main__":
    run()