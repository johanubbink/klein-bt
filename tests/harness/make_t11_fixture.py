"""Regenerate ``tests/fixtures/t11_filelogger2.btlog`` from a real t11 run.

    python -m tests.harness.make_t11_fixture [SECONDS]

Runs ``T11Target`` for SECONDS (default 11: two CrossDoor missions of ~3.7 s
with t11's 2 s sleep between them), then keeps the FileLogger2 file up to the
end of the last *complete* mission — the root Sequence going back to IDLE — so
the file is whole records only and a ``--replay`` loop of it is seamless.
"""
import sys
import time
from pathlib import Path

from tests.harness import btlog_ref
from tests.harness.targets import T11Target

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "t11_filelogger2.btlog"
ROOT_UID = 1


def cut_to_last_mission(data):
    log = btlog_ref.parse(data)
    ends = [i for i, (_, uid, st) in enumerate(log.records) if uid == ROOT_UID and st == 0]
    if not ends:
        raise RuntimeError("no complete mission in the recording; run it longer")
    return btlog_ref.build(log.xml, log.first_timestamp_us, log.records[:ends[-1] + 1],
                           version=log.version)


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 11.0
    with T11Target() as t11:
        time.sleep(seconds)
        data = t11.btlog_bytes()
    out = cut_to_last_mission(data)
    FIXTURE.write_bytes(out)
    log = btlog_ref.parse(out)
    print(f"wrote {FIXTURE}: {len(log.records)} records, {len(log.xml)}-byte XML, "
          f"span {log.records[-1][0] / 1e6:.3f} s")


if __name__ == "__main__":
    main()
