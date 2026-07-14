"""End-to-end sync test: two simulated Kodi instances pair, then sync
subscriptions + groups through the Noise-protected record protocol.

This exercises the full record opcodes (PUBLISH, LIST_RECORD_KEYS,
GET_RECORD) over a real TCP socket, with real Noise_N envelope encryption.
"""
import base64
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from resources.lib.sync.crypto import x25519
from resources.lib.sync import session as sync_session, records
from resources.lib.sync.service import _generate_pairing_code, _local_ips


def _stub_profile(profile_dir):
    os.environ["GRAYJAY_PROFILE_OVERRIDE"] = profile_dir
    from resources.lib import kodiutils
    kodiutils.profile_path = lambda: profile_dir
    kodiutils.get_setting = lambda key, default="": os.environ.get("GRAYJAY_" + key.upper(), default)
    kodiutils.set_setting = lambda key, value: os.environ.__setitem__("GRAYJAY_" + key.upper(), value)


def run():
    a_dir = tempfile.mkdtemp()
    b_dir = tempfile.mkdtemp()

    a_priv, a_pub = x25519.generate_keypair()
    b_priv, b_pub = x25519.generate_keypair()
    pairing = _generate_pairing_code()

    a_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    a_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    a_listener.bind(("127.0.0.1", 0))
    a_listener.listen(1)
    a_port = a_listener.getsockname()[1]

    # Server (a) thread: accepts and does the responder handshake.
    accepted = {}

    def a_server():
        sock, addr = a_listener.accept()
        try:
            sync_session.write_version(sock)
            def check(code):
                return code == pairing
            transport, remote_pub, received_code = sync_session.respond_ik_handshake(
                sock, a_priv, check_pairing=check)
            sess = sync_session.SyncSocketSession(sock, "%s:%d" % addr)
            sess.local_public_key = a_pub
            sess.remote_public_key = remote_pub
            sess.transport = transport
            sess.authorized = True
            sync_session.install_record_server(sess, a_priv)
            accepted["sess"] = sess
            # Authorize and run a receive loop in another thread so requests get answered.
            def on_data(s, opc, sub, pl):
                pass

            def on_close(s):
                pass

            threading.Thread(target=sess.receive_loop, args=(on_data, on_close),
                             daemon=True).start()
        except Exception as exc:
            print("a_server failed:", exc)
            sock.close()

    threading.Thread(target=a_server, daemon=True).start()

    # Client (b) connects and initiates.
    b_sock = socket.create_connection(("127.0.0.1", a_port), timeout=5)
    sync_session.read_version(b_sock)
    hs, _ = sync_session.initiate_ik_handshake(b_sock, b_priv, a_pub, pairing_code=pairing)
    msg2 = sync_session.initiate_recv_ik_msg2(b_sock)
    _, b_transport = hs.read_message(msg2)

    b_sess = sync_session.SyncSocketSession(b_sock, "client")
    b_sess.local_public_key = b_pub
    b_sess.remote_public_key = a_pub
    b_sess.transport = b_transport
    b_sess.authorized = True
    sync_session.install_record_server(b_sess, b_priv)

    def on_data_b(s, opc, sub, pl):
        pass

    def on_close_b(s):
        pass

    threading.Thread(target=b_sess.receive_loop, args=(on_data_b, on_close_b),
                     daemon=True).start()

    for _ in range(50):
        if "sess" in accepted:
            break
        time.sleep(0.05)
    a_sess = accepted["sess"]
    assert a_sess is not None, "a_sess not established"

    # ====== Now exercise the record protocol ======

    # Build subscriptions payloads.
    a_subs = [{"source": "yt", "url": "channel-A", "name": "Channel A", "thumbnail": ""},
              {"source": "yt", "url": "channel-B", "name": "Channel B", "thumbnail": ""}]
    b_subs = [{"source": "yt", "url": "channel-C", "name": "Channel C", "thumbnail": ""}]

    # B pushes its subs to A.
    b_payload = json.dumps(b_subs).encode("utf-8")
    req_id = records.publish_to(b_sess, base64.b64encode(a_pub).decode("ascii"),
                                 "subscriptions", b_payload)
    print("b publish req_id:", req_id)
    # Wait briefly for A's response to be routed.
    time.sleep(0.3)

    # A pulls B's records.
    pulled = records.fetch_from(a_sess, a_priv,
                                 base64.b64encode(b_pub).decode("ascii"),
                                 base64.b64encode(a_pub).decode("ascii"),
                                 "subscriptions")
    print("a pulled from b:", pulled[1].decode("utf-8") if pulled else None)
    assert pulled is not None, "a failed to pull subscriptions from b"
    decoded_b = json.loads(pulled[1].decode("utf-8"))
    assert decoded_b == b_subs

    # A pushes its subs to B.
    a_payload = json.dumps(a_subs).encode("utf-8")
    req_id = records.publish_to(a_sess, base64.b64encode(b_pub).decode("ascii"),
                                 "subscriptions", a_payload)
    print("a publish req_id:", req_id)
    time.sleep(0.3)

    # B pulls A's records.
    pulled2 = records.fetch_from(b_sess, b_priv,
                                  base64.b64encode(a_pub).decode("ascii"),
                                  base64.b64encode(b_pub).decode("ascii"),
                                  "subscriptions")
    print("b pulled from a:", pulled2[1].decode("utf-8") if pulled2 else None)
    assert pulled2 is not None
    decoded_a = json.loads(pulled2[1].decode("utf-8"))
    assert decoded_a == a_subs

    # ===== Groups ======
    a_groups = [{"id": "music", "name": "Music",
                 "members": [{"source": "yt", "url": "channel-A"}]}]
    b_groups = [{"id": "tech", "name": "Tech",
                 "members": [{"source": "yt", "url": "channel-C"}]}]

    a_g_payload = json.dumps(a_groups).encode("utf-8")
    records.publish_to(a_sess, base64.b64encode(b_pub).decode("ascii"),
                       "groups", a_g_payload)
    time.sleep(0.3)
    pulled_g = records.fetch_from(b_sess, b_priv,
                                   base64.b64encode(a_pub).decode("ascii"),
                                   base64.b64encode(b_pub).decode("ascii"),
                                   "groups")
    print("b pulled groups from a:", pulled_g[1].decode("utf-8") if pulled_g else None)
    assert pulled_g is not None
    assert json.loads(pulled_g[1].decode("utf-8")) == a_groups

    b_g_payload = json.dumps(b_groups).encode("utf-8")
    records.publish_to(b_sess, base64.b64encode(a_pub).decode("ascii"),
                       "groups", b_g_payload)
    time.sleep(0.3)
    pulled_g2 = records.fetch_from(a_sess, a_priv,
                                    base64.b64encode(b_pub).decode("ascii"),
                                    base64.b64encode(a_pub).decode("ascii"),
                                    "groups")
    print("a pulled groups from b:", pulled_g2[1].decode("utf-8") if pulled_g2 else None)
    assert pulled_g2 is not None
    assert json.loads(pulled_g2[1].decode("utf-8")) == b_groups

    print("RECORD SYNC: ALL OK")


if __name__ == "__main__":
    run()