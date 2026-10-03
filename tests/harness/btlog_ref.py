"""An independent reference reader, writer and replayer for FileLogger2 ``.btlog`` files.

Written from ``BehaviorTree.CPP/src/loggers/bt_file_logger_v2.cpp`` and
``groot2_publisher.cpp`` alone, and deliberately importing nothing from
``klein``: the feature's own ``klein/btlog.py`` is checked *against* this, so the
two must not share code (a shared bug would pass every comparison).

File layout (all little-endian)::

    b"BTCPP4-FileLogger2"  u8 version (1)  i32 xml_len  xml  i64 first_timestamp_us
    then N x 9-byte records: u48 offset_us (from first_timestamp) | u16 uid | u8 status

Record statuses are live NodeStatus values (IDLE is a plain 0). Replayed
state uses the STATUS-reply encoding instead: a node that goes IDLE is stored
as ``10 + previous live status``, the publisher's rule.
"""
import collections
import struct
import xml.etree.ElementTree as ET

MAGIC = b"BTCPP4-FileLogger2"
VERSION = 1
RECORD_SIZE = 9
IDLE_TRANSITION = 10
STATUS_NAMES = {0: "IDLE", 1: "RUNNING", 2: "SUCCESS", 3: "FAILURE", 4: "SKIPPED"}

Btlog = collections.namedtuple("Btlog", "version xml first_timestamp_us records trailing")
Btlog.__doc__ = ("A parsed file. ``records`` are ``(offset_us, uid, status)``; "
                 "``trailing`` counts the bytes of an incomplete last record.")


def parse(data):
    """Parse ``.btlog`` bytes into a ``Btlog``. Whole records only."""
    data = bytes(data)
    if not data.startswith(MAGIC):
        raise ValueError("not a FileLogger2 .btlog")
    pos = len(MAGIC)
    version = data[pos]
    pos += 1
    (xml_len,) = struct.unpack_from("<i", data, pos)
    pos += 4
    xml = data[pos:pos + xml_len].decode("utf-8")
    pos += xml_len
    (first,) = struct.unpack_from("<q", data, pos)
    pos += 8
    body = data[pos:]
    whole = len(body) - len(body) % RECORD_SIZE
    records = []
    for off in range(0, whole, RECORD_SIZE):
        ts = int.from_bytes(body[off:off + 6], "little")
        uid, status = struct.unpack_from("<HB", body, off + 6)
        records.append((ts, uid, status))
    return Btlog(version, xml, first, records, len(body) - whole)


def read(path):
    with open(path, "rb") as f:
        return parse(f.read())


def build(xml, first_timestamp_us, records, version=VERSION):
    """Serialize a ``.btlog`` — for building test inputs and known-bad variants."""
    body = xml.encode("utf-8")
    out = bytearray(MAGIC + bytes([version]) + struct.pack("<i", len(body)) + body
                    + struct.pack("<q", first_timestamp_us))
    for ts, uid, status in records:
        out += int(ts).to_bytes(6, "little") + struct.pack("<HB", uid, status)
    return bytes(out)


def record_offset(log, index):
    """Byte offset of record ``index`` in the file ``log`` was parsed from."""
    return len(MAGIC) + 1 + 4 + len(log.xml.encode("utf-8")) + 8 + index * RECORD_SIZE


def absolute(log):
    """The records as ``(absolute_us, uid, status)``."""
    return [(log.first_timestamp_us + ts, uid, st) for ts, uid, st in log.records]


def tree_uids(xml):
    """Every ``_uid`` in a tree XML, ascending."""
    return sorted({int(el.get("_uid")) for el in ET.fromstring(xml).iter() if el.get("_uid")})


def apply(state, uid, status):
    """Apply one live-status transition to a STATUS-encoded ``{uid: byte}`` state."""
    if status == 0:
        prev = state.get(uid, 0)
        state[uid] = IDLE_TRANSITION + (prev if prev < IDLE_TRANSITION else 0)
    else:
        state[uid] = status


def replay(records, uids=(), until=None):
    """STATUS-encoded state after every record with time <= ``until`` (all, if None).

    ``records`` may be offsets or absolute times, as long as ``until`` matches.
    Every uid in ``uids`` starts at 0, the state of a freshly created tree.
    """
    state = {uid: 0 for uid in uids}
    for ts, uid, status in records:
        if until is not None and ts > until:
            break
        apply(state, uid, status)
    return state


def states_at_record_times(records, uids=()):
    """``{time: state}`` for every distinct record time — the state once *all*
    records sharing that time have been applied."""
    out = {}
    state = {uid: 0 for uid in uids}
    for i, (ts, uid, status) in enumerate(records):
        apply(state, uid, status)
        if i + 1 == len(records) or records[i + 1][0] != ts:
            out[ts] = dict(state)
    return out


def decode(state):
    """A STATUS-encoded state as ``{uid: {"status", "from"}}``, the shape
    ``groot2_protocol.parse_status`` returns and the dashboard paints."""
    out = {}
    for uid, value in state.items():
        if value >= IDLE_TRANSITION:
            out[uid] = {"status": "IDLE", "from": STATUS_NAMES.get(value - IDLE_TRANSITION)}
        else:
            out[uid] = {"status": STATUS_NAMES.get(value, "UNKNOWN"), "from": None}
    return out
