"""klein.cli — the ``klein-bt`` command: parse the flags, build the gateway
(``klein/gateway.py``) and run it until Ctrl-C."""

import argparse
import asyncio
import math
import socket
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from .btlog import BtlogError
from .gateway import KleinGateway
from .recording import Recording


def _duration_us(text):
    """``--record-buffer`` value -> µs: ``90s``, ``10m``, ``1h``; a bare number
    is seconds, and ``0`` turns recording off."""
    units = {"s": 1, "m": 60, "h": 3600}
    text = text.strip().lower()
    scale = units.get(text[-1:])
    try:
        value = float(text[:-1] if scale else text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"not a duration: {text!r} (try 90s, 10m, 1h)")
    if value < 0:
        raise argparse.ArgumentTypeError("the duration must not be negative")
    return round(value * (scale or 1) * 1_000_000)


def _port_available(port):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Match the asyncio server's bind semantics (SO_REUSEADDR): reject a port
    # only when a live listener holds it, not when harmless TIME_WAIT sockets
    # linger from a just-closed session (which would block an immediate restart).
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def main_cli():
    parser = argparse.ArgumentParser(
        prog="klein-bt",
        description="Live BehaviorTree.CPP v4 telemetry dashboard over ZeroMQ.",
    )
    parser.add_argument("--robot-host", default="127.0.0.1",
                        help="IP address of the C++ robot node (default: 127.0.0.1)")
    parser.add_argument("--robot-port", type=int, default=1667,
                        help="ZeroMQ REQ/REP port on the robot (default: 1667)")
    parser.add_argument("--port", type=int, default=8080,
                        help="port for the klein dashboard + telemetry server (default: 8080)")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not auto-open the system browser")
    parser.add_argument("--record-buffer", type=_duration_us, default="10m",
                        metavar="DURATION",
                        help="how much recent history to record, e.g. 90s, 30m, 1h; 0 turns "
                             "recording off and polls the robot only while a dashboard is "
                             "open (default: 10m)")
    parser.add_argument("--debug", action="store_true",
                        help="serve a read-only JSON dump of the recording at /debug/state")
    parser.add_argument("--open", metavar="FILE.btlog", type=Path,
                        help="show a saved FileLogger2 recording (and its FILE.bb.jsonl "
                             "blackboard, if present) instead of a robot")
    args = parser.parse_args()

    recording = None
    if args.open is None and args.record_buffer:
        # Size caps are Recording's defaults: 200 MiB in all, 64 MiB of it blackboard.
        recording = Recording(keep_us=args.record_buffer)
    gateway = KleinGateway(args.robot_host, args.robot_port, args.port,
                           recording=recording, debug=args.debug)
    if args.open is not None:
        try:
            trailing = gateway.open_file(args.open)
        except (OSError, BtlogError, ET.ParseError, ValueError) as exc:
            print(f"[klein] cannot open {args.open}: {exc}", file=sys.stderr)
            sys.exit(1)
        if trailing:
            print(f"[klein] {args.open}: ignored a partial last record "
                  f"({trailing} byte{'s' if trailing > 1 else ''}).")

    if not _port_available(args.port):
        print(f"[klein] port {args.port} is already in use — try a different --port.",
              file=sys.stderr)
        sys.exit(1)

    print()
    print("  klein — BehaviorTree.CPP telemetry")
    if args.open is not None:
        print(f"  file      : {args.open} (no robot)")
    else:
        print(f"  robot     : tcp://{args.robot_host}:{args.robot_port}")
    print(f"  dashboard : http://localhost:{args.port}")
    print()

    try:
        # The browser is opened from inside run(), once the server is listening,
        # so it never races ahead of the socket being ready.
        asyncio.run(gateway.run(open_browser=not args.no_browser))
    except KeyboardInterrupt:
        print("\n[klein] shutting down.")


if __name__ == "__main__":
    main_cli()
