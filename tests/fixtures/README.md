# Test fixtures

The two `.btlog` files are FileLogger2 recordings (the format is in
[docs/protocol.md](../../docs/protocol.md#recorded-files-btlog)). Both hold
whole records only. Treat them as read-only: tests compare against them.

## `groot2_mock.btlog`

Written by **Groot2** itself (its "log to file" button), connected to
`klein-bt-mock` (CrossDoor tree) for a ~110 s run on 2026-09-30.

- 616 transitions, 2183-byte XML equal to `mock_robot.CROSSDOOR_XML`,
  no trailing bytes.
- Its first timestamp is exactly the mock's `r start` reply, and its records
  are exactly the `t` replies Groot2 received (checked through a wire tap).

## `t11_filelogger2.btlog`

Written by the **real BehaviorTree.CPP** `t11_groot_howto` example's own
`FileLogger2` (not by klein or Groot2), running the port-argument build the
harness makes (`tests/harness/build_t11.sh`). Regenerate with:

```bash
python -m tests.harness.make_t11_fixture      # runs T11Target ~11 s
```

The script keeps the file up to the end of the last complete mission (the root
Sequence's final IDLE), so a `--replay` of it loops seamlessly.

- 90 transitions: two CrossDoor missions (~3.7 s each, 2 s apart), span 9.40 s.
- 9334-byte XML with a full `<TreeNodesModel>` (builtins included), 13 uids.
- Shows what the mock does not: synchronous nodes going IDLE → SUCCESS
  without RUNNING, PickLock's `FAILURE → IDLE → RUNNING` retries within ~60 µs,
  and bursts of records sharing a microsecond.

A regenerated file differs in its timestamps (and so in bytes); CrossDoor is
deterministic, so its (uid, status) sequence should not change. Tests must not
depend on its exact timestamps.
