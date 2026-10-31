"""Unit tests for klein.robot_link: a ``RobotLink`` against a real ZeroMQ REP
socket in this process, answering as a Groot2 publisher does."""
import asyncio
import struct
import unittest

import zmq
import zmq.asyncio

from klein.groot2_protocol import (HEADER_FORMAT, PROTOCOL_ID, REQ_BLACKBOARD, REQ_FULLTREE,
                                   REQ_STATUS, REQ_TOGGLE_RECORDING)
from klein.robot_link import REQUEST_TIMEOUT, Reply, RobotLink, RobotRefused, RobotTimeout
from tests.helpers import UUID_A, reply_header


class RobotLinkTest(unittest.IsolatedAsyncioTestCase):
    async def test_requests_in_turn(self):
        """One link, one publisher, each request after the last. A request
        with no answer times out, and the next one must go through: the link
        re-created its stuck REQ socket."""
        ctx = zmq.asyncio.Context()
        self.addCleanup(ctx.destroy, linger=0)
        rep = ctx.socket(zmq.REP)
        rep.bind("tcp://127.0.0.1:*")
        link = RobotLink(rep.getsockopt_string(zmq.LAST_ENDPOINT), ctx=ctx)
        self.addCleanup(link.close)
        status = struct.pack("<HBHB", 1, 1, 2, 0)
        cases = [
            # label, (request, argument frame), the publisher's answer, expected
            ("a reply", (REQ_STATUS, None),
             [reply_header(UUID_A), status], Reply(UUID_A, status)),
            ("an argument frame", (REQ_BLACKBOARD, b"MainTree"),
             [reply_header(UUID_A, REQ_BLACKBOARD), b"\xc0"], Reply(UUID_A, b"\xc0")),
            ("no answer", (REQ_STATUS, None), None, RobotTimeout),
            ("the next request recovers", (REQ_FULLTREE, None),
             [reply_header(UUID_A, REQ_FULLTREE), b"<root/>"], Reply(UUID_A, b"<root/>")),
            # groot2_publisher.cpp :: sendErrorReply
            ("an error frame", (REQ_STATUS, None),
             [b"error", b"Request not recognized"], RobotRefused),
            # `r stop` is answered with the header alone.
            ("no payload frame", (REQ_TOGGLE_RECORDING, b"stop"),
             [reply_header(UUID_A, REQ_TOGGLE_RECORDING)], RobotRefused),
        ]
        for label, (kind, payload), answer, expected in cases:
            with self.subTest(label):
                # Short only where no answer comes, so no other case can time out.
                link.timeout = 0.05 if answer is None else REQUEST_TIMEOUT
                pending = asyncio.ensure_future(link.request(kind, payload))
                frames = await asyncio.wait_for(rep.recv_multipart(), REQUEST_TIMEOUT)
                self.assertEqual(struct.unpack(HEADER_FORMAT, frames[0])[:2], (PROTOCOL_ID, kind))
                self.assertEqual(frames[1:], [] if payload is None else [payload])
                if answer is None:
                    with self.assertRaises(RobotTimeout):
                        await pending
                    # REP must answer before it can take the next request; this
                    # goes to the socket the link has thrown away.
                    await rep.send_multipart([b"too late"])
                    continue
                await rep.send_multipart(answer)
                if isinstance(expected, Reply):
                    self.assertEqual(await pending, expected)
                else:
                    with self.assertRaises(expected):
                        await pending


if __name__ == "__main__":
    unittest.main()
