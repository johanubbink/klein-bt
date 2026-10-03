// The JS runner's self-test subject: a plain <script>-style file (no export),
// shaped like the dashboard's own modules (recording.js, cursor.js). It
// applies transitions with the publisher's rule, as tests/harness/btlog_ref.py
// does, and returns a Map and a Set for the runner's container comparisons.

const IDLE_TRANSITION = 10;

function applyTransition(state, uid, status) {
    const prev = state[uid] || 0;
    state[uid] = status === 0 ? IDLE_TRANSITION + (prev < IDLE_TRANSITION ? prev : 0) : status;
    return state;
}

function replay(records) {
    const state = {};
    for (const [, uid, status] of records) applyTransition(state, uid, status);
    return state;
}

function makeStore() {
    const records = [];
    return {
        append(t, uid, status) { records.push([t, uid, status]); return records.length; },
        stateAt(t) { return replay(records.filter((r) => r[0] <= t)); },
    };
}

// Per-uid transition counts, as a Map, and the set of uids touched: the two
// container types plain JSON would flatten to {}.
function countByUid(records) {
    const counts = new Map();
    for (const [, uid] of records) counts.set(uid, (counts.get(uid) || 0) + 1);
    return counts;
}

function uidsTouched(records) {
    return new Set(records.map((r) => r[1]));
}

const KleinSelfTest = { applyTransition, replay, makeStore, countByUid, uidsTouched };
if (typeof module !== "undefined") module.exports = KleinSelfTest;
else window.KleinSelfTest = KleinSelfTest;
