"""klein.robot_link — the one ZeroMQ REQ socket to a Groot2 publisher.

The publisher binds a ``ZMQ_REP`` socket (docs/protocol.md); klein keeps one
``ZMQ_REQ`` socket to it, and every request (the handshake's, both pollers',
the recording's) goes through ``RobotLink.request``. It hides what the wire
needs and the callers don't:

* REQ/REP is strictly send→recv, so requests are serialised on one lock;
* a request with no reply within ``timeout`` leaves the REQ socket stuck
  mid-exchange, so the socket is thrown away and a fresh one opened;
* the request header (protocol id, request type, a random unique id);
* the reply's shape: frame 0 is the reply header carrying the tree UUID,
  frame 1 the payload, and a refused request comes back as
  ``[b"error", <message>]``.

What callers see is a ``Reply`` or one of two exceptions: ``RobotTimeout``
(no answer, which is evidence of an outage) or ``RobotRefused`` (an answer,
but without a payload).
"""

import asyncio
import collections
import random
import struct

import zmq
import zmq.asyncio

from .groot2_protocol import HEADER_FORMAT, PROTOCOL_ID, decode_tree_uuid

REQUEST_TIMEOUT = 2.0           # seconds to wait for a robot reply

# A reply with a payload: the publisher's tree UUID from its header (None when
# the header is too short to hold one) and frame 1, the payload bytes.
Reply = collections.namedtuple("Reply", "tree_uuid body")


class RobotTimeout(Exception):
    """No reply within the timeout, or the REQ socket faulted."""


class RobotRefused(Exception):
    """The robot answered without a payload: an error frame (a request it
    can't serve, such as ``r`` on BehaviorTree.CPP < 4.3.3) or a reply
    missing frame 1."""


class RobotLink:
    """One REQ socket to a Groot2 publisher: serialised, timed out, re-created after a fault.

    ``ctx`` is the ZeroMQ context to open the socket in; without one the link
    makes its own on first use and ``close`` terminates it. Nothing is opened
    until the first request, so a link that is never used holds nothing.
    """

    def __init__(self, endpoint, timeout=REQUEST_TIMEOUT, ctx=None):
        self.endpoint = endpoint
        self.timeout = timeout
        self._ctx = ctx
        self._owns_ctx = ctx is None
        self._socket = None                 # created lazily / recreated on fault
        # Made on first use, inside the loop that uses it: constructing a link
        # must not require a running event loop. On Python 3.9 asyncio.Lock()
        # reaches for get_event_loop() and raises without one.
        self._lock = None                   # REQ/REP is strictly send→recv

    async def request(self, kind, payload=None):
        """Send one request and return its ``Reply``.

        ``kind`` is a request type (``REQ_STATUS``, …). Some requests carry an
        argument frame after the header (BLACKBOARD the boards to dump,
        TOGGLE_RECORDING ``start``); pass it as ``payload``. Raises
        ``RobotTimeout`` (the socket has then been re-created) or
        ``RobotRefused``.
        """
        if self._lock is None:
            # No await between the check and the assignment, so concurrent
            # callers on the one event loop cannot both make a lock.
            self._lock = asyncio.Lock()
        async with self._lock:
            frames = await self._exchange(kind, payload)
        if frames[0] == b"error":
            message = frames[1].decode("utf-8", errors="replace") if len(frames) > 1 else ""
            raise RobotRefused(f"robot replied with an error frame {message!r}")
        if len(frames) < 2:
            raise RobotRefused("reply missing its payload frame")
        return Reply(decode_tree_uuid(frames[0]), frames[1])

    async def _exchange(self, kind, payload):
        """One send→recv on the socket: the reply's frames."""
        if self._socket is None:
            self._open()
        header = struct.pack(HEADER_FORMAT, PROTOCOL_ID, kind, random.randint(1, 0xFFFFFFFF))
        frames = [header] if payload is None else [header, payload]
        try:
            await self._socket.send_multipart(frames)
            return await asyncio.wait_for(self._socket.recv_multipart(), timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            self._open()
            raise RobotTimeout(f"no reply within {self.timeout:g}s") from exc
        except zmq.ZMQError as exc:
            self._open()
            raise RobotTimeout(str(exc) or "socket error") from exc

    def _open(self):
        """(Re)create the REQ socket. A timed-out REQ socket is stuck in the
        wrong half of its send/recv state machine and cannot be reused, so it
        is thrown away and a clean one connected."""
        if self._socket is not None:
            self._socket.close(linger=0)
        if self._ctx is None:
            self._ctx = zmq.asyncio.Context()
        self._socket = self._ctx.socket(zmq.REQ)
        self._socket.setsockopt(zmq.LINGER, 0)
        self._socket.connect(self.endpoint)

    def close(self):
        """Close the socket, and the context if the link made it."""
        if self._socket is not None:
            self._socket.close(linger=0)
            self._socket = None
        if self._owns_ctx and self._ctx is not None:
            self._ctx.term()
            self._ctx = None
