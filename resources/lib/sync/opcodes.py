# Sync opcodes. Verbatim from the desktop client (SyncShared/Opcode.cs)
# so the wire format is interoperable with the existing Grayjay app.
#
# Wire format (after the Noise_IK handshake):
#     [4 bytes BE? No, LE int32 size][Noise-encrypted(size bytes payload)]
# Payload plaintext:
#     [4 bytes int32 size] [1 byte opcode] [1 byte sub-opcode]
#     [1 byte content encoding] [size-7 bytes payload]
#
# Sub-opcodes depend on the main opcode.


class Opcode:
    PING = 0
    PONG = 1
    NOTIFY = 2
    STREAM = 3
    DATA = 4
    REQUEST = 5
    RESPONSE = 6
    RELAY = 7


class NotifyOpcode:
    AUTHORIZED = 0
    UNAUTHORIZED = 1
    CONNECTION_INFO = 2     # not used in our LAN-only setup
    DEVICE_TOKEN = 3        # not used
    SET_NOTIFICATION_ALLOW_LIST = 4  # not used
    PUSH_NOTIFICATION = 5   # not used


class StreamOpcode:
    START = 0
    DATA = 1
    END = 2


class RequestOpcode:
    CONNECTION_INFO = 0
    TRANSPORT = 1
    TRANSPORT_RELAYED = 2
    PUBLISH_RECORD = 3
    DELETE_RECORD = 4
    LIST_RECORD_KEYS = 5
    GET_RECORD = 6
    BULK_PUBLISH_RECORD = 7
    BULK_GET_RECORD = 8
    BULK_CONNECTION_INFO = 9
    BULK_DELETE_RECORD = 10


class ResponseOpcode:
    CONNECTION_INFO = 0
    TRANSPORT = 1
    TRANSPORT_RELAYED = 2
    PUBLISH_RECORD = 3
    DELETE_RECORD = 4
    LIST_RECORD_KEYS = 5
    GET_RECORD = 6
    BULK_PUBLISH_RECORD = 7
    BULK_GET_RECORD = 8
    BULK_CONNECTION_INFO = 9
    BULK_DELETE_RECORD = 10


class ContentEncoding:
    RAW = 0
    GZIP = 1


# Sync-specific sub-opcodes (GJSyncOpcodes). On the desktop these live in
# Grayjay.ClientServer.Sync.Internal. We use the same integer values so the
# wire format is byte-identical to the desktop's SyncExport / SyncStateExchange.
class GJSyncOpcode:
    SYNC_EXPORT = 0          # full export zip (desktop only)
    SYNC_STATE_EXCHANGE = 1  # delta of changed records
    SYNC_CONFIG_SYNC = 2     # settings sync (desktop only)