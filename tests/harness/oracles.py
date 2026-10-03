"""The comparisons the integration and UI tests share. Pure functions, no I/O.

Each returns an ``OracleResult``: truthy when the check holds, and always
carrying a human-readable ``detail`` — what was compared when it passed, the
first mismatch when it did not — so a test can do
``self.assertTrue(r, r.detail)`` and a failure explains itself.
"""
import re

from tests.harness import btlog_ref

# klein's times are the ``r start`` reply plus the robot's own offsets, so
# klein - truth is one clock-read difference: constant. A real t11 shows 0-1 µs
# of spread (µs truncation); anything more is a timing bug, not jitter.
MAX_SPREAD_US = 5


class OracleResult:
    def __init__(self, ok, detail):
        self.ok = bool(ok)
        self.detail = detail

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return f"OracleResult({'PASS' if self.ok else 'FAIL'}: {self.detail})"


def _fail(detail):
    return OracleResult(False, detail)


def transitions_match(klein_records, truth_records, max_offset_us=50, from_time=None):
    """klein's recorded transitions equal the target's ground truth.

    Both are ``[(absolute_us, uid, live_status)]``. klein's first record is
    paired with the nearest truth record of the same (uid, status); from there
    the (uid, status) sequences must be identical up to klein's last record,
    and the offset ``klein - truth`` must be at most ``max_offset_us`` and
    constant (``MAX_SPREAD_US``). The target running on after klein stopped is
    not a mismatch; a klein record the truth does not have — even past the
    truth's end — is. (So a truth that lags must be read after it caught up:
    ``T11Target.ground_truth()`` waits out FileLogger2's flush.)

    ``from_time`` is klein's arm time (its ``r start`` reply): every truth
    record from ``from_time + max_offset_us`` on is then owed, so a record
    dropped at the start fails too. Within ``max_offset_us`` of the arm either
    side of the boundary is legitimate — the robot stamps the reply and its
    record base with two clock reads.
    """
    klein = sorted(klein_records, key=lambda r: r[0])
    truth = sorted(truth_records, key=lambda r: r[0])
    if not klein or not truth:
        return _fail(f"nothing to compare: {len(klein)} klein, {len(truth)} truth records")
    floor = -float("inf") if from_time is None else from_time - max_offset_us
    candidates = [j for j, r in enumerate(truth) if r[1:] == klein[0][1:] and r[0] >= floor]
    if not candidates:
        return _fail(f"klein's first record {klein[0]} never occurs in the truth")
    j = min(candidates, key=lambda j: abs(klein[0][0] - truth[j][0]))
    c = klein[0][0] - truth[j][0]                   # the offset this alignment implies
    if from_time is not None:
        skipped = [r for r in truth[:j] if r[0] >= from_time + max_offset_us]
        if skipped:
            return _fail(f"klein lacks {len(skipped)} truth record(s) after the arm, before "
                         f"its first; first missing: {skipped[0]}")
    # klein owes every truth record up to where klein stops (with the spread as
    # slack: a real robot's offset jitters by 1 µs, two µs truncations). Every
    # klein record must pair with one, so a record past the truth's end fails.
    k = klein
    t = [r for r in truth[j:] if r[0] <= klein[-1][0] - c + MAX_SPREAD_US]
    for i, (kr, tr) in enumerate(zip(k, t)):
        if kr[1:] != tr[1:]:
            return _fail(f"record #{i}: klein (uid, status) {kr[1:]} at {kr[0]} != truth "
                         f"{tr[1:]} at {tr[0]}\n  klein around it: {k[max(0, i - 2):i + 3]}"
                         f"\n  truth around it: {t[max(0, i - 2):i + 3]}")
    if len(t) != len(k):
        return _fail(f"klein has {len(k)} records where the truth has {len(t)}; first "
                     f"unpaired: {(t if len(t) > len(k) else k)[min(len(k), len(t))]}")
    offsets = [kr[0] - tr[0] for kr, tr in zip(k, t)]
    worst = max(offsets, key=abs)
    if abs(worst) > max_offset_us:
        return _fail(f"timestamp offset {worst} us exceeds {max_offset_us} us")
    if max(offsets) - min(offsets) > MAX_SPREAD_US:
        return _fail(f"timestamp offset not constant: {min(offsets)}..{max(offsets)} us")
    return OracleResult(True, f"{len(k)} records match from truth index {j}; offset "
                              f"{min(offsets)}..{max(offsets)} us")


def state_matches(observed, reference, allow_was_vs_idle=True):
    """Node-by-node equality of two ``{uid: {"status", "from"}}`` states.

    ``observed`` is what klein shows (the DOM, a debug dump), ``reference`` what
    it should (a STATUS reply, a replayed ground truth). With
    ``allow_was_vs_idle``, "IDLE, was X" and plain IDLE (``from`` None) count
    as equal: they differ only in history a snapshot may not know. Two "was"
    values must still agree — "was SUCCESS" is never "was FAILURE".
    """
    obs = {int(k): v for k, v in observed.items()}
    ref = {int(k): v for k, v in reference.items()}
    uids = sorted(set(obs) | set(ref))
    if not uids:
        return _fail("both states are empty")
    bad = []
    for uid in uids:
        o, r = obs.get(uid), ref.get(uid)
        if o == r:
            continue
        if (allow_was_vs_idle and o and r and o["status"] == r["status"] == "IDLE"
                and (o["from"] is None or r["from"] is None)):
            continue
        bad.append(f"uid {uid}: observed {o} != reference {r}")
    if bad:
        return _fail(f"{len(bad)} of {len(uids)} nodes differ: " + "; ".join(bad))
    return OracleResult(True, f"{len(uids)} nodes match")


def state_in_frames(observed, frames):
    """``observed`` equals (strictly, as ``state_matches`` without the
    was-vs-IDLE allowance) at least one of ``frames``: status snapshots around
    the moment it was read, e.g. one poll either side."""
    results = [state_matches(observed, frame, allow_was_vs_idle=False) for frame in frames]
    for i, result in enumerate(results):
        if result:
            return OracleResult(True, f"matches frame {i + 1} of {len(frames)}")
    return _fail(f"matches none of {len(frames)} frames: "
                 + " | ".join(r.detail for r in results))


def btlog_equivalent(a_bytes, b_bytes):
    """Two ``.btlog`` files describe the same run.

    Header (version, first timestamp) and XML equal, and replaying each file
    gives the identical STATUS-encoded state at every record time of either —
    so records reordered within one µs are equivalent, but a changed uid,
    status or time is not.
    """
    a, b = btlog_ref.parse(a_bytes), btlog_ref.parse(b_bytes)
    if (a.version, a.first_timestamp_us) != (b.version, b.first_timestamp_us):
        return _fail(f"header differs: (version, first timestamp) "
                     f"{(a.version, a.first_timestamp_us)} != {(b.version, b.first_timestamp_us)}")
    if a.xml != b.xml:
        return _fail("XML differs")
    uids = btlog_ref.tree_uids(a.xml)
    sa = btlog_ref.states_at_record_times(a.records, uids)
    sb = btlog_ref.states_at_record_times(b.records, uids)
    times = sorted(set(sa) | set(sb))
    state_a = state_b = btlog_ref.replay([], uids)
    for t in times:
        state_a, state_b = sa.get(t, state_a), sb.get(t, state_b)
        if state_a != state_b:
            diff = {u: (state_a[u], state_b[u]) for u in uids if state_a[u] != state_b[u]}
            return _fail(f"replayed state differs at +{t} us: {{uid: (a, b)}} = {diff}")
    return OracleResult(True, f"{len(a.records)} vs {len(b.records)} records; state equal "
                              f"at all {len(times)} record times")


def request_sequence_matches(seq, pattern):
    """The whole request sequence, e.g. ``"TSSSS"`` from
    ``WireTap.request_sequence()``, matches a regex over request letters, e.g.
    ``r"T(S)+"`` or ``r"TrS(St)+"``."""
    shown = seq if len(seq) <= 120 else f"{seq[:60]}…{seq[-60:]} ({len(seq)} requests)"
    if re.fullmatch(pattern, seq):
        return OracleResult(True, f"{shown!r} matches {pattern!r}")
    return _fail(f"{shown!r} does not match {pattern!r}")
