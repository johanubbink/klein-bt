"""klein.recorder — the recording session: one publisher, recorded into a ``Recording``.

The ``Recording`` (klein/recording.py) is the pure model; the ``Recorder``
feeds it over the gateway's ``RobotLink``. The gateway says when (after each
handshake, after each good status poll, on each blackboard sample and each
timeout); the Recorder decides what that means for the recording. See
docs/architecture.md, "Recording", and for the wire side docs/protocol.md,
"How klein records".

* ``arm``: ``r start``, a STATUS baseline, then a new segment, after an
  ``outage`` gap if the robot went away. A publisher that refuses ``r``
  (BehaviorTree.CPP < 4.3.3) can't record: the open segment ends and none
  begins until the next handshake tries again.
* ``drain``: one ``t`` into the open segment; a drain of the publisher's cap
  adds an ``overflow`` gap and re-arms. Then eviction, and the head moves.
* ``note_timeout`` and ``robot_lost``: the evidence of an outage. The open
  segment ends at the head, the last time klein heard from the robot.

It hides the session's state (armed, a publisher that can't record, a
timeout since the last good poll) and the mapping of klein's monotonic
clock onto the robot's µs.
"""

import sys
import time

from .groot2_protocol import (
    RECORDING_START,
    REQ_GET_TRANSITIONS,
    REQ_STATUS,
    REQ_TOGGLE_RECORDING,
    TRANSITION_BUFFER_MAX,
    decode_transitions,
    iter_status,
)
from .robot_link import RobotRefused


class Recorder:
    """Records one robot, over ``link``, into ``recording``.

    ``recording`` None is ``--record-buffer 0``: nothing is ever sent or
    recorded, and ``state`` is ``"off"``. ``clock`` is klein's monotonic
    clock in seconds (injected by the tests).
    """

    def __init__(self, recording, link, clock=time.monotonic):
        self.recording = recording
        self.armed = False                  # an open segment is taking `t` drains
        self._link = link
        self._clock = clock
        self._cannot_record = False         # this publisher answered `r` with an error
        # A request (any poller's, or the handshake's) timed out since the last
        # good status poll: if the robot turns out to have gone, the open
        # segment ends at the head, the last time klein heard from it.
        self._timed_out = False
        self._tree = None                   # the layout the armed segment records
        # The last `r start`: its reply (the armed segment's t_begin, robot µs)
        # and klein's clock at the midpoint of its round trip, taken to be
        # when the robot read its clock.
        self._start_us = None
        self._mid = None

    @property
    def state(self):
        """What the drawer says about recording: ``"on"``, ``"unsupported"``
        (the publisher answered ``r`` with an error) or ``"off"``."""
        if self.recording is None:
            return "off"
        return "unsupported" if self._cannot_record else "on"

    def _robot_us(self, mono):
        """klein's clock reading ``mono`` in the robot's µs."""
        return self._start_us + round((mono - self._mid) * 1e6)

    async def arm(self, tree):
        """Begin a segment of ``tree``: ``r start``, then a STATUS baseline.

        Run on every handshake and after an overflow. The ``r start`` reply is
        the robot's wall-clock µs, the base every drained offset is added to.
        In that order nothing is lost between the two: whatever ran after
        ``r start`` is in the robot's buffer, and what ran before the ``S`` is
        in the baseline too, so the first drain replays it on top of the
        baseline (``apply_transition`` makes that replay end on the baseline).
        ``RobotTimeout`` propagates, and leaves the recorder unarmed.
        """
        if self.recording is None:
            return
        self.armed = False
        self._cannot_record = False         # a new handshake may be a new publisher
        sent = self._clock()
        try:
            reply = await self._link.request(REQ_TOGGLE_RECORDING, RECORDING_START.encode())
        except RobotRefused:
            self._unsupported(self._clock())
            return
        received = self._clock()
        start_us = int(reply.body)
        try:
            baseline = await self._baseline(tree)
        except RobotRefused:
            return                          # no baseline: unarmed until the next handshake
        self._begin(tree, start_us, (sent + received) / 2, baseline)

    def _unsupported(self, received):
        """The publisher answered ``r`` with an error: it can't record. klein
        records nothing from it, and sends it no ``t``, until the next
        handshake tries again."""
        self._cannot_record = True
        if self.recording.open_segment is not None:
            self.recording.end_segment(self._robot_us(received))
        print("[klein] robot cannot record transitions (needs BehaviorTree.CPP "
              ">= 4.3.3); not recording.", file=sys.stderr)

    async def _baseline(self, tree):
        """A STATUS reply as a uid-indexed state of ``tree``'s size."""
        reply = await self._link.request(REQ_STATUS)
        baseline = bytearray(tree.size)
        for uid, status in iter_status(reply.body):
            if uid < tree.size:
                baseline[uid] = status
        return baseline

    def _begin(self, tree, start_us, mid, baseline):
        rec = self.recording
        if self._timed_out:
            # A request timed out (a blackboard poll, say) and the robot came
            # back as a new publisher before any status poll timed out: that
            # was an outage too.
            rec.end_at_head()
        self._timed_out = False
        if rec.ended_at is not None:        # ended when the robot went away
            rec.add_gap(rec.ended_at, start_us, "outage")
        self._tree, self._start_us, self._mid = tree, start_us, mid
        rec.begin_segment(tree, start_us, baseline)
        self.armed = True

    async def drain(self):
        """After every good status poll: any timeout before it was a blip.
        While armed, one ``t`` drain into the open segment. A drain of exactly
        the publisher's cap means the oldest were dropped: an overflow gap,
        and a re-arm, so the next segment starts from a fresh baseline."""
        self._timed_out = False
        if not self.armed:
            return
        try:
            reply = await self._link.request(REQ_GET_TRANSITIONS)
        except RobotRefused:
            return
        records = [(self._start_us + offset, uid, status)
                   for offset, uid, status in decode_transitions(reply.body)]
        if len(records) == TRANSITION_BUFFER_MAX:
            self.recording.append_after_overflow(records)
            await self.arm(self._tree)
        else:
            self.recording.append(records)
        now = self._robot_us(self._clock())
        self.recording.evict(now)           # first, so the head frame has its sizes
        self.recording.advance_head(now)

    def add_blackboard(self, received, boards):
        """Add a ``{board: {key: value}}`` sample, received at klein's clock
        ``received``, to the armed segment at the robot's time. Evicted at
        once, not at the next drain: a dashboard that connects in between
        gets sizes within the caps."""
        if not self.armed:
            return
        now = self._robot_us(received)
        self.recording.add_blackboard(now, boards)
        self.recording.evict(now)

    def note_timeout(self):
        """A request timed out: the robot may have gone. A good status poll
        (``drain``) clears this; an ``arm`` before one ends the open segment
        at the head and adds the outage gap."""
        self._timed_out = True

    def robot_lost(self):
        """A status poll timed out: end the open segment at the head, the last
        time klein heard from the robot. The re-handshake when telemetry
        resumes arms again, after the outage gap from that end."""
        self.armed = False
        if self.recording is not None:
            self.recording.end_at_head()
