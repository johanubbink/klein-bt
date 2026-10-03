"""klein.btlog — FileLogger2 ``.btlog`` files, read and written in memory.

The format BehaviorTree.CPP's ``FileLogger2`` writes and Groot2 opens
(``bt_file_logger_v2.cpp``; the header comment in ``bt_file_logger_v2.h``
leaves out the magic and the version byte — the .cpp is authoritative).
All little-endian::

    b"BTCPP4-FileLogger2" | u8 version (1) | i32 xml_len | xml (UTF-8)
    | i64 first_timestamp (µs since the epoch)
    | N x 9-byte records: u48 offset_us (from first_timestamp) | u16 uid | u8 status

A record is exactly a GET_TRANSITIONS record, so the packing is shared with
``groot2_protocol``. Statuses are live NodeStatus ints (IDLE is a plain 0).

``export_run`` and ``export_blackboard`` save one tree run of a ``Recording``
(docs/protocol.md, "Saving a recording"); ``load_btlog`` turns a file and its
sidecar back into one (``klein-bt --open``, docs/protocol.md, "Opening a file").
"""

import collections
import itertools
import json
import struct

from .groot2_protocol import TRANSITION_RECORD_SIZE, decode_transitions, encode_transition
from .recording import Recording, apply_transition, diff_to_transitions

MAGIC = b"BTCPP4-FileLogger2"
VERSION = 1
_XML_OFFSET = len(MAGIC) + 1 + 4        # the XML starts after magic, version, xml_len
_TIMESTAMP_SIZE = 8

BtlogFile = collections.namedtuple("BtlogFile", "xml first_timestamp records trailing")
BtlogFile.__doc__ = ("A parsed ``.btlog``. ``records`` are ``(offset_us, uid, status)``; "
                     "``trailing`` counts the bytes of an incomplete last record.")


class BtlogError(ValueError):
    """The data is not a FileLogger2 ``.btlog`` klein can read."""


def read_btlog(data):
    """Parse ``.btlog`` bytes into a ``BtlogFile``.

    A trailing partial record — a robot stopped mid-write — is left out of
    ``records`` and counted in ``trailing``, not raised.
    """
    if not data.startswith(MAGIC):
        raise BtlogError("not a FileLogger2 .btlog (no BTCPP4-FileLogger2 magic)")
    version = data[len(MAGIC):len(MAGIC) + 1]
    if version != bytes([VERSION]):
        shown = version[0] if version else "missing"
        raise BtlogError(f"unknown .btlog format version {shown}")
    xml_len = int.from_bytes(data[len(MAGIC) + 1:_XML_OFFSET], "little", signed=True)
    if not 0 <= xml_len <= len(data) - _XML_OFFSET - _TIMESTAMP_SIZE:
        raise BtlogError(f"XML length {xml_len} does not fit the file")
    body = _XML_OFFSET + xml_len + _TIMESTAMP_SIZE     # where the records start
    xml = data[_XML_OFFSET:_XML_OFFSET + xml_len].decode("utf-8")
    (first_timestamp,) = struct.unpack_from("<q", data, body - _TIMESTAMP_SIZE)
    records = data[body:]
    return BtlogFile(xml, first_timestamp, decode_transitions(records),
                     len(records) % TRANSITION_RECORD_SIZE)


def _header(xml, first_timestamp):
    body = xml.encode("utf-8")
    return b"".join([MAGIC, bytes([VERSION]), struct.pack("<i", len(body)), body,
                     struct.pack("<q", first_timestamp)])


def write_btlog(xml, first_timestamp, records):
    """Serialize a ``.btlog``; ``records`` are ``(offset_us, uid, status)``."""
    return b"".join([_header(xml, first_timestamp)]
                    + [encode_transition(*record) for record in records])


def export_run(run, xml):
    """One tree run (``Recording.runs()``) as ``.btlog`` bytes.

    ``first_timestamp`` is the run's retained start. Replayed from all-IDLE, as
    Groot2 and klein start a file, the file gives the recorded state at every
    retained time: each segment opens with the transitions from the state
    before it (all-IDLE for the first) to its retained start — the prefix at
    offset 0, and at each restart or re-arm inside the run.

    A record ``apply_transition`` ignores (an IDLE on a node already idle: an
    arm-overlap record the baseline already held) is left out, so a reader
    with the plain publisher rule never sees "was IDLE".
    """
    first = run[0].t_start
    state = bytearray(len(run[0].baseline))     # the file's state, as written so far
    out = bytearray(_header(xml, first))
    for segment in run:
        start = segment.state_at_seq(segment.start_seq)
        opening = [(segment.t_start, uid, status)
                   for uid, status in diff_to_transitions(state, start)]
        recorded = ((ts, uid, status) for _seq, ts, uid, status
                    in segment.iter_records(segment.start_seq, segment.head_seq))
        for ts, uid, status in itertools.chain(opening, recorded):
            if apply_transition(state, uid, status):
                out += encode_transition(ts - first, uid, status)
    return bytes(out)


def export_blackboard(run, tree_id):
    """One tree run's blackboard history as ``.bb.jsonl`` bytes: a header line,
    then one line per change at its offset from the run's retained start.

    Each segment opens with the lines that turn the previous values (none, for
    the first) into its values at its retained start, so the file stands alone
    after eviction and across restarts.
    """
    first = run[0].t_start
    lines = [json.dumps({"klein_blackboard": 1, "first_timestamp": first, "tree_id": tree_id})]
    values = {}                             # (board, key) -> JSON text, as of the last line

    def line(t, board, key, text):
        entry = json.dumps({"t": t - first, "board": board, "key": key})[:-1]
        if text is None:
            del values[board, key]
            lines.append(f'{entry}, "removed": true}}')
        else:
            values[board, key] = text       # the stored JSON text goes in as it is
            lines.append(f'{entry}, "value": {text}}}')

    for segment in run:
        track = segment.blackboard
        if track.t_start is None:
            continue
        base = max(track.t_start, first)
        at_base, later = {}, []
        for t, board, key, text in track.changes():
            if t <= base:
                at_base[board, key] = text
            else:
                later.append((t, board, key, text))
        for name in [name for name in values if at_base.get(name) is None]:
            line(base, *name, None)
        for name, text in at_base.items():
            if text is not None and values.get(name) != text:
                line(base, *name, text)
        for change in later:
            line(*change)
    return "".join(entry + "\n" for entry in lines).encode("utf-8")


def load_btlog(log, layout, boards=(), sidecar=None):
    """A ``Recording`` of one parsed file (``read_btlog``), as the gateway
    would have recorded it: one ended segment from ``first_timestamp``,
    replayed from all-IDLE as Groot2 starts a file, with its head at the last
    record.

    ``layout`` is the segment's layout (``.size``: the state length, max uid
    + 1). ``sidecar`` is the ``.bb.jsonl`` text (``export_blackboard``), or
    None. Its lines become blackboard samples holding every board in
    ``boards`` (the layout's, in tree order), so a board with no lines (no
    keys of its own) still shows, empty, as it does live. The first sample is
    at the first line, or at the sidecar's start when it has none.
    """
    recording = Recording()
    segment = recording.begin_segment(layout, log.first_timestamp, bytes(layout.size))
    recording.append([(log.first_timestamp + offset, uid, status)
                      for offset, uid, status in log.records])
    if sidecar is not None:
        _load_blackboard(recording, sidecar, boards)
    recording.end_segment(segment.last_ts)
    recording.advance_head(segment.last_ts)
    return recording


def _load_blackboard(recording, text, boards):
    header, *lines = [json.loads(line) for line in text.splitlines() if line.strip()]
    first = header["first_timestamp"]
    values = {board: {} for board in boards}
    by_time = collections.defaultdict(list)
    for line in lines:
        by_time[line["t"]].append(line)
    for t in sorted(by_time) or [0]:
        for line in by_time[t]:
            entries = values.setdefault(line["board"], {})
            if line.get("removed"):
                entries.pop(line["key"], None)
            else:
                entries[line["key"]] = line["value"]
        recording.add_blackboard(first + t, values)
