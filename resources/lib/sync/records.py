# -*- coding: utf-8 -*-
"""Local record store + sync orchestration.

Each record is a small JSON blob saved under <profile>/sync/records/<key>.json
of the form {"timestamp": <int unix ms>, "data": <bytes-as-base64 or str>}.

The "publisher" of a record is the public key of the device that wrote it.
The "consumer" is the public key that is allowed to fetch it — only that
consumer's static key can decrypt the record blob (we encrypt each record
with Noise_N keyed to the consumer's static pubkey at publish time).

For our two-way sync of subscriptions + groups, both devices act as both
publisher and consumer of the other's records:

    - We (local) publish "subscriptions" and "groups" records targeted at
      each paired device (per-consumer blobs).
    - We list+fetch the peer's "subscriptions" and "groups" records they
      published for us.
    - On conflict, newest timestamp wins (the record whose local JSON file
      has a higher `timestamp` survives; if equal we keep our local copy).

This is a strict subset of the desktop's record-store API — enough for
subscriptions + groups, but not for the full backup / state-exchange flow.
"""
import base64
import glob
import json
import os
import struct
import threading
import time

from ..kodiutils import profile_path, log


_SYNC_DIR = None
_RECORDS_DIR = None
_LOCK = threading.Lock()


def _sync_dir():
    global _SYNC_DIR
    if _SYNC_DIR is None:
        _SYNC_DIR = os.path.join(profile_path(), "sync")
    if not os.path.isdir(_SYNC_DIR):
        os.makedirs(_SYNC_DIR)
    return _SYNC_DIR


def _records_dir():
    global _RECORDS_DIR
    if _RECORDS_DIR is None:
        _RECORDS_DIR = os.path.join(_sync_dir(), "records")
    if not os.path.isdir(_RECORDS_DIR):
        os.makedirs(_RECORDS_DIR)
    return _RECORDS_DIR


def _path(key):
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in key)
    return os.path.join(_records_dir(), safe + ".json")


def load(key):
    """Load the local record for `key`, or None if absent / corrupt."""
    p = _path(key)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        log("record load failed for %s: %s" % (key, exc), "warning")
        return None


def save(key, data_bytes, timestamp=None):
    """Save a record. data_bytes is raw bytes; we base64-encode for JSON.
    timestamp is unix milliseconds; defaults to now()."""
    if timestamp is None:
        timestamp = int(time.time() * 1000)
    rec = {
        "timestamp": int(timestamp),
        "data": base64.b64encode(bytes(data_bytes)).decode("ascii"),
    }
    p = _path(key)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    os.replace(tmp, p)


def delete(key):
    p = _path(key)
    try:
        os.remove(p)
    except FileNotFoundError:
        pass


def list_local_keys():
    """Return the set of record keys we have on disk."""
    return [os.path.basename(p)[:-5] for p in glob.glob(os.path.join(_records_dir(), "*.json"))]


# -- high-level sync helpers -------------------------------------------------

def publish_to(session, consumer_pub_b64, key, data_bytes, timestamp=None):
    """Encrypt `data_bytes` to `consumer_pub_b64` (Noise_N) and PUBLISH_RECORD
    it under `key` with the given timestamp. Also updates our local record
    store so the same data is served to peers that pull from us later."""
    from . import session as _session
    if timestamp is None:
        timestamp = int(time.time() * 1000)
    save(key, data_bytes, timestamp=timestamp)
    payload = struct_pack_timestamp(timestamp) + bytes(data_bytes)
    return _session.publish_record(session, consumer_pub_b64, key, payload)


def fetch_from(session, priv_local, publisher_pub_b64, consumer_pub_b64, key):
    """LIST+GET `key` records that `publisher_pub_b64` has published for us.
    Returns (timestamp_ms, data_bytes) or None if no such record."""
    from . import session as _session
    req_id = _session.list_record_keys(session, publisher_pub_b64, consumer_pub_b64)
    _, keys = _session.wait_for_response(session, req_id, lambda p: _session.parse_list_response(p))
    matched = [(k, ts, sz) for (k, ts, sz) in keys if k == key]
    if not matched:
        return None
    _, ts, _ = matched[0]
    req_id = _session.get_record(session, publisher_pub_b64, key)
    _, blob, ts_resp = _session.wait_for_response(
        session, req_id, lambda p: _session.parse_get_response(p, priv_local))
    if blob is None:
        return None
    if not blob or len(blob) < 8:
        return None
    # Strip the leading 8-byte timestamp and return raw data.
    timestamp_ms = struct.unpack(">q", blob[:8])[0]
    return timestamp_ms, blob[8:]


def struct_pack_timestamp(ms):
    import struct
    return struct.pack(">q", int(ms))


def _local_pub_b64():
    """Read our local static public key from the SyncService's keypair file."""
    p = os.path.join(_sync_dir(), "keypair.json")
    if not os.path.isfile(p):
        raise RuntimeError("sync keypair not generated yet")
    with open(p, "r", encoding="utf-8") as fh:
        kp = json.load(fh)
    return kp["public_key"]


def sync_pair(session, priv_local, peer_pub_b64, key, local_data_getter, local_data_setter):
    """One-way newest-wins sync of a single named record.

    - `local_data_getter()` -> bytes (or None if no local record)
    - `local_data_setter(data_bytes)` -> persists + triggers UI reload

    Pulls peer's record (if any), pushes ours. Resolves conflict by timestamp.
    Returns "pushed", "pulled", "both-equal", or "skipped" depending on outcome."""
    # Pull peer's
    pulled = fetch_from(session, priv_local, peer_pub_b64, key)
    # Push ours
    local_bytes = local_data_getter()
    local_ts = int(time.time() * 1000)
    if local_bytes is not None:
        publish_to(session, peer_pub_b64, key, local_bytes, timestamp=local_ts)

    if pulled is None:
        if local_bytes is not None:
            return "pushed"
        return "skipped"

    pulled_ts, pulled_bytes = pulled
    local = load(key)
    local_ts_actual = (local or {}).get("timestamp", 0)
    if local is None:
        # We have nothing; accept theirs.
        save(key, pulled_bytes, timestamp=pulled_ts)
        local_data_setter(pulled_bytes)
        return "pulled"
    if pulled_ts > local_ts_actual:
        save(key, pulled_bytes, timestamp=pulled_ts)
        local_data_setter(pulled_bytes)
        return "pulled"
    return "skipped"