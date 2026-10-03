// recording.js — the browser's mirror of the gateway's Recording.
//
// klein streams its recording over the WebSocket (klein/streaming.py): a
// backfill on connect, then every change. A Store ingests those messages and
// holds the same segments, chunks, keyframes, seqs and blackboard history as
// klein/recording.py, so everything a past moment needs (state, blackboard,
// log rows, timeline bars) is computed here and scrubbing never asks the
// gateway anything. The query functions mirror the Python ones exactly; the
// vectors tests/make_vectors.py builds from Python are checked against this
// file under gjs.
//
// No DOM and no d3: plain functions over plain data, so it runs under gjs.
// See docs/architecture.md, "Browser model".

(function () {
"use strict";

// Groot2 status bytes, as in klein/groot2_protocol.py.
const IDLE = 0, RUNNING = 1, SUCCESS = 2, FAILURE = 3, SKIPPED = 4;
const IDLE_TRANSITION = 10;
const STATUS_NAMES = ["IDLE", "RUNNING", "SUCCESS", "FAILURE", "SKIPPED"];

// An outcome: the status a finished tick leaves.
function isOutcome(status) {
    return status === SUCCESS || status === FAILURE || status === SKIPPED;
}

// The binary records frame (klein/streaming.py), little-endian.
const RECORDS_CHUNK_START = 1;
const RECORDS_APPEND = 2;
const RECORDS_HEADER_SIZE = 23;     // u8 kind, u32 seg, u32 seq0, u32 first seq, i64 base, u16 keyframe len
const RECORD_SIZE = 9;              // u48 offset from base, u16 uid, u8 status

const REMOVED = undefined;          // a blackboard removal; JSON values are never undefined

// ------------------------------------------------------------------ //
// State encoding
// ------------------------------------------------------------------ //
// The publisher's callback rule: IDLE is stored as 10 + the previous live status.
// An IDLE on a node already idle changes nothing (see apply_transition in
// recording.py: arming replays records the baseline already holds).
function applyTransition(state, uid, status) {
    if (status === IDLE) {
        const current = state[uid];
        if (current > 0 && current < IDLE_TRANSITION) state[uid] = IDLE_TRANSITION + current;
    } else {
        state[uid] = status;
    }
}

function decodeStatus(value) {
    if (value >= IDLE_TRANSITION) {
        const previous = STATUS_NAMES[value - IDLE_TRANSITION];
        return previous === undefined ? ["UNKNOWN", null] : ["IDLE", previous];
    }
    return [STATUS_NAMES[value] || "UNKNOWN", null];
}

// {uid: {status, from}} for uids: the shape groot2_protocol.parse_status sends.
function decodeState(state, uids) {
    const out = {};
    for (const uid of uids) {
        const [status, from] = decodeStatus(state[uid]);
        out[uid] = { status, from };
    }
    return out;
}

// The number of a[lo..hi) that are <= x, a sorted (by key(i)).
function bisectRight(hi, x, key) {
    let lo = 0;
    while (lo < hi) {
        const mid = (lo + hi) >>> 1;
        if (key(mid) <= x) lo = mid + 1;
        else hi = mid;
    }
    return lo;
}

// ------------------------------------------------------------------ //
// Chunks, segments, blackboard tracks, recordings
// ------------------------------------------------------------------ //
class Chunk {
    constructor(seq0, keyframe) {
        this.seq0 = seq0;
        this.keyframe = keyframe;           // Uint8Array: the state before the first record
        this.length = 0;
        this.ts = new Float64Array(64);     // absolute µs: exact in a double
        this.uid = new Uint16Array(64);
        this.status = new Uint8Array(64);
    }

    push(t, uid, status) {
        if (this.length === this.ts.length) {
            for (const name of ["ts", "uid", "status"]) {
                const grown = new this[name].constructor(this.length * 2);
                grown.set(this[name]);
                this[name] = grown;
            }
        }
        this.ts[this.length] = t;
        this.uid[this.length] = uid;
        this.status[this.length] = status;
        this.length++;
    }
}

// Per-key blackboard changes of one segment, as BlackboardTrack in recording.py.
class BlackboardTrack {
    constructor() { this.clear(); }

    clear() {
        this.tStart = null;
        this.boards = new Map();            // board -> time first seen, in that order
        this.keys = new Map();              // "board\0key" -> {board, key, ts: [], values: []}
    }

    add(t, changes, removed, boards) {
        if (this.tStart === null) this.tStart = t;
        for (const board of boards) if (!this.boards.has(board)) this.boards.set(board, t);
        const put = (board, key, value) => {
            const name = board + "\0" + key;
            let entry = this.keys.get(name);
            if (!entry) this.keys.set(name, entry = { board, key, ts: [], values: [] });
            entry.ts.push(t);
            entry.values.push(value);
        };
        for (const [board, key, value] of changes) put(board, key, value);
        for (const [board, key] of removed) put(board, key, REMOVED);
    }

    at(t) {
        if (this.tStart === null || t < this.tStart) return null;
        const out = {};
        for (const [board, seen] of this.boards) if (seen <= t) out[board] = {};
        for (const { board, key, ts, values } of this.keys.values()) {
            const i = bisectRight(ts.length, t, (j) => ts[j]) - 1;
            if (i >= 0 && values[i] !== REMOVED) out[board][key] = values[i];
        }
        return out;
    }

    // Python's evict_before: drop changes older than t, keeping per key the
    // latest one at or before t.
    evictBefore(t) {
        if (this.tStart === null || t <= this.tStart) return;
        this.tStart = t;
        for (const [name, entry] of this.keys) {
            let i = bisectRight(entry.ts.length, t, (j) => entry.ts[j]) - 1;
            if (i >= 0 && entry.values[i] === REMOVED) i++;
            if (i > 0) {
                entry.ts.splice(0, i);
                entry.values.splice(0, i);
            }
            if (entry.ts.length === 0) this.keys.delete(name);
        }
    }
}

class Segment {
    constructor(msg, layout) {
        this.id = msg.seg;
        this.layoutId = msg.layout_id;
        this.layout = layout;               // shared with the previous segment of the same tree
        this.uids = msg.uids;
        this.tBegin = msg.t_begin;
        this.tEnd = null;
        this.tStart = msg.t_begin;          // moved up by eviction
        this.headSeq = msg.start_seq;
        this.state = Uint8Array.from(msg.state);    // the state at the head
        this.chunks = [];
        this.bb = new BlackboardTrack();
        this.bbDropped = false;             // eviction has cut its blackboard history
    }

    // The first retained seq (stateAtSeq answers from here to headSeq).
    get startSeq() {
        return this.chunks.length ? this.chunks[0].seq0 : this.headSeq;
    }
}

class Recording {
    constructor(msg) {
        this.source = msg.source;           // "robot", or "file" (klein-bt --open)
        this.name = msg.name;
        this.keepUs = msg.keep_us;
        this.segments = [];
        this.gaps = [];                     // {tFrom, tTo, kind}
        this.head = null;
        this.bytes = null;
        this.capped = [];
        this.backfillDone = false;
    }

    get tMin() {
        return this.segments.length ? this.segments[0].tStart : null;
    }

    segment(id) {
        return this.segments.find((s) => s.id === id) || null;
    }
}

// ------------------------------------------------------------------ //
// Store: ingests the gateway's recording messages
// ------------------------------------------------------------------ //
class Store {
    constructor() {
        this.recording = null;              // the one recording klein streams, once it said so
    }

    // One WebSocket message: a parsed text frame or a binary ArrayBuffer.
    // Returns true when it was a recording message (and so consumed).
    ingest(msg) {
        if (msg instanceof ArrayBuffer) {
            if (this.recording) ingestRecords(this.recording, msg);
            return true;
        }
        if (msg.type === "rec") {           // a (re)connect: rebuild from scratch
            this.recording = new Recording(msg);
            return true;
        }
        const handler = INGEST[msg.type];
        if (!handler) return false;
        if (this.recording) handler(this.recording, msg);
        return true;
    }
}

// One binary records frame into rec.
function ingestRecords(rec, buffer) {
    const view = new DataView(buffer);
    const kind = view.getUint8(0);
    const seg = rec.segment(view.getUint32(1, true));
    if (!seg) return;
    const seq0 = view.getUint32(5, true);
    const base = Number(view.getBigInt64(13, true));
    const keyframeLength = view.getUint16(21, true);
    let at = RECORDS_HEADER_SIZE;
    let chunk;
    if (kind === RECORDS_CHUNK_START) {
        chunk = new Chunk(seq0, new Uint8Array(buffer.slice(at, at + keyframeLength)));
        seg.chunks.push(chunk);
    } else {
        chunk = seg.chunks[seg.chunks.length - 1];
    }
    at += keyframeLength;
    const n = view.getUint32(at, true);
    at += 4;
    for (let i = 0; i < n; i++, at += RECORD_SIZE) {
        const offset = view.getUint32(at, true) + view.getUint16(at + 4, true) * 2 ** 32;
        const uid = view.getUint16(at + 6, true);
        const status = view.getUint8(at + 8);
        chunk.push(base + offset, uid, status);
        applyTransition(seg.state, uid, status);
    }
    seg.headSeq += n;
}

const INGEST = {
    segment(rec, msg) {
        const previous = rec.segments[rec.segments.length - 1];
        const layout = "layout" in msg ? msg.layout : previous.layout;
        rec.segments.push(new Segment(msg, layout));
    },
    segment_end(rec, msg) {
        const seg = rec.segment(msg.seg);
        if (seg) seg.tEnd = msg.t_end;
    },
    gap(rec, msg) {
        rec.gaps.push({ tFrom: msg.t_from, tTo: msg.t_to, kind: msg.kind });
    },
    bb(rec, msg) {
        const seg = rec.segment(msg.seg);
        if (seg) seg.bb.add(msg.t, msg.changes, msg.removed, msg.boards);
    },
    head(rec, msg) {
        rec.head = msg.t;
        rec.bytes = msg.bytes;              // [transitions, blackboard], for the drawer
        rec.capped = msg.capped;            // histories a size cap has cut short
    },
    // Drop what the gateway dropped: whole segments, leading chunks, old
    // blackboard changes. The frame says what is left; nothing is recomputed.
    evict(rec, msg) {
        const kept = new Map(msg.segments.map((s) => [s[0], s]));
        rec.segments = rec.segments.filter((seg) => kept.has(seg.id));
        for (const seg of rec.segments) {
            const [, startSeq, tStart, bbStart] = kept.get(seg.id);
            while (seg.chunks.length && seg.chunks[0].seq0 < startSeq) seg.chunks.shift();
            seg.tStart = tStart;
            const bbBefore = seg.bb.tStart;
            if (bbStart === null) seg.bb.clear();
            else seg.bb.evictBefore(bbStart);
            if (bbBefore !== null && seg.bb.tStart !== bbBefore) seg.bbDropped = true;
        }
        rec.gaps = rec.gaps.filter((gap) => msg.t_min !== null && gap.tTo >= msg.t_min);
    },
    backfill_done(rec) {
        rec.backfillDone = true;
    },
};

// ------------------------------------------------------------------ //
// Queries (mirroring Segment / BlackboardTrack in recording.py)
// ------------------------------------------------------------------ //
// The chunk holding seq (startSeq <= seq < headSeq).
function chunkOf(seg, seq) {
    const i = bisectRight(seg.chunks.length, seq, (j) => seg.chunks[j].seq0) - 1;
    return seg.chunks[i];
}

// The state after the first seq records, or null if evicted.
function stateAtSeq(seg, seq) {
    if (seq < seg.startSeq) return null;
    if (seq >= seg.headSeq) return seg.state.slice();
    const chunk = chunkOf(seg, seq);
    const state = chunk.keyframe.slice();
    for (let i = 0; i < seq - chunk.seq0; i++) applyTransition(state, chunk.uid[i], chunk.status[i]);
    return state;
}

// The number of records with ts <= t (meaningful for t >= tStart).
function seqAtTime(seg, t) {
    const i = bisectRight(seg.chunks.length, t, (j) => seg.chunks[j].ts[0]);
    if (i === 0) return seg.startSeq;
    const chunk = seg.chunks[i - 1];
    return chunk.seq0 + bisectRight(chunk.length, t, (j) => chunk.ts[j]);
}

// The state once every record with ts <= t applied, or null if evicted.
function stateAt(seg, t) {
    if (t < seg.tStart) return null;
    return stateAtSeq(seg, seqAtTime(seg, t));
}

// The time the cursor at seq shows: its last applied record's, or tStart.
function timeAtSeq(seg, seq) {
    if (seq <= seg.startSeq) return seg.tStart;
    const chunk = chunkOf(seg, seq - 1);
    return chunk.ts[seq - 1 - chunk.seq0];
}

// [[seq, ts, uid, status]] for the retained seqs in [a, b) (the Log rows).
function recordsRange(seg, a, b) {
    const out = [];
    for (const chunk of seg.chunks) {
        const lo = Math.max(a, chunk.seq0) - chunk.seq0;
        const hi = Math.min(b, chunk.seq0 + chunk.length) - chunk.seq0;
        for (let i = lo; i < hi; i++) {
            out.push([chunk.seq0 + i, chunk.ts[i], chunk.uid[i], chunk.status[i]]);
        }
    }
    return out;
}

// [{t0, t1, status}]: uid's state byte over [t0, t1] (the Timeline bars), one
// entry per value it held, in order. A value held for no time (two records in
// one µs) is a zero-length entry. t0 is clamped to the retained start.
function intervals(seg, uid, t0, t1) {
    return intervalsAll(seg, [uid], t0, t1)[uid];
}

// {uid: intervals(seg, uid, t0, t1)} for every uid in uids, in one pass over
// the records: the Timeline asks for every node at once.
function intervalsAll(seg, uids, t0, t1) {
    t0 = Math.max(t0, seg.tStart);
    const out = {};
    if (t0 > t1) {
        for (const uid of uids) out[uid] = [];
        return out;
    }
    const state = stateAt(seg, t0);
    const start = {};
    for (const uid of uids) {
        out[uid] = [];
        start[uid] = t0;
    }
    const a = seqAtTime(seg, t0), b = seqAtTime(seg, t1);
    for (const chunk of seg.chunks) {
        const lo = Math.max(a, chunk.seq0) - chunk.seq0;
        const hi = Math.min(b, chunk.seq0 + chunk.length) - chunk.seq0;
        for (let i = lo; i < hi; i++) {
            const uid = chunk.uid[i];
            const value = state[uid];
            applyTransition(state, uid, chunk.status[i]);
            if (!(uid in out) || state[uid] === value) continue;
            out[uid].push({ t0: start[uid], t1: chunk.ts[i], status: value });
            start[uid] = chunk.ts[i];
        }
    }
    for (const uid of uids) out[uid].push({ t0: start[uid], t1, status: state[uid] });
    return out;
}

// A layout tree's name: the main tree's ID (the root node's name without one).
function treeName(layout) {
    return layout.root_tree_id || layout.name;
}

// fn(node) for every node of a d3 hierarchy, parents first, folded away or
// not: a folded node keeps its children in _children (app.js).
function eachNode(root, fn) {
    (function walk(node) {
        fn(node);
        for (const child of node.children || node._children || []) walk(child);
    })(root);
}

// What the Timeline draws for one node's intervals:
//   bars     [{t0, t1, end}]  one per RUNNING interval; end is the outcome
//                             (2 SUCCESS, 3 FAILURE, 4 SKIPPED) that ended it,
//                             or null (still running, or halted to IDLE)
//   marks    [{t, status}]    an outcome reached without RUNNING first: the
//                             node finished in the tick it started
//   failures [t]              every FAILURE it entered (a section header's marks)
//   changes  [t]              every change of its state (a folded row's marks)
// The first interval's value was set before the window, so it is no change.
function timelineMarks(ivs) {
    const out = { bars: [], marks: [], failures: [], changes: [] };
    ivs.forEach((iv, i) => {
        const next = ivs[i + 1];
        if (iv.status === RUNNING) {
            const end = next && isOutcome(next.status) ? next.status : null;
            out.bars.push({ t0: iv.t0, t1: iv.t1, end });
        }
        if (i === 0) return;
        out.changes.push(iv.t0);
        if (iv.status === FAILURE) out.failures.push(iv.t0);
        if (isOutcome(iv.status) && ivs[i - 1].status !== RUNNING) {
            out.marks.push({ t: iv.t0, status: iv.status });
        }
    });
    return out;
}

// The Timeline's rows for one layout tree: one section per subtree, in tree
// order. The root opens the main tree's section and every SubTree node a
// section of its own; a section is its header node's row followed by the rows
// below it, nested sections included where they occur. Each item:
//   {id, uid, name, type, category, depth, tint, section, label, count,
//    expandable, folded, hidden, inner, context, items}
// depth is the tree depth, tint the subtree nesting (0 main tree, as the
// canvas fills); count the nodes below it; inner the uids below it; hidden
// how many of those a fold hides. Folded nodes (ids in `folded`) hide
// what is below them. A filter (`needle`) keeps the nodes whose name or
// subtree name contains it, ignoring case, as the Log does, folded or not;
// a section kept only for what it holds is `context`. Null when nothing matches.
function timelineSections(tree, folded, needle = "") {
    folded = new Set(folded);           // the ids, as any iterable
    needle = needle.trim().toLowerCase();
    const matches = (name, subtree) => !needle || name.toLowerCase().includes(needle)
                                                || subtree.toLowerCase().includes(needle);
    function walk(node, depth, tint, subtree, shown) {
        const section = depth === 0 || Boolean(node.is_subtree_root);
        if (node.is_subtree_root) {
            subtree = node.name;
            tint++;
        }
        const children = node.children || [];
        const isFolded = folded.has(node.id) && children.length > 0;
        const kids = children.map((c) => walk(c, depth + 1, tint, subtree, shown && !isFolded));
        const inner = [];
        let count = 0;
        for (const k of kids) {
            count += 1 + k.count;
            if (k.uid !== null && k.uid !== undefined) inner.push(k.uid);
            inner.push(...k.inner);
        }
        const item = {
            id: node.id, uid: node.uid === undefined ? null : node.uid, name: node.name,
            type: node.type, category: node.category, depth,
            tint: Math.min(tint, 3), section,
            label: depth === 0 ? treeName(tree) : node.name,
            count, expandable: children.length > 0, folded: isFolded,
            hidden: isFolded ? count : 0, inner, context: false, items: [],
            keep: matches(node.name, subtree) && (shown || Boolean(needle)),
        };
        // What follows this node's row: the rows below it, flattened into its
        // own section or, for a header, kept inside it.
        item.flat = [];
        for (const k of kids) {
            if (!(needle || (shown && !isFolded))) break;
            if (k.section) {
                if (k.keep || k.items.length) item.flat.push(k);
            } else {
                if (k.keep) item.flat.push(k);
                item.flat.push(...k.flat);
            }
        }
        if (section) {
            item.items = item.flat;
            item.flat = [];
            if (!item.keep && item.items.length) {
                item.context = true;
                item.keep = true;
            }
        }
        return item;
    }
    const root = walk(tree, 0, 0, treeName(tree), true);
    return root.keep ? strip(root) : null;
}

// Drop timelineSections' working fields.
function strip(item) {
    const { keep, flat, ...rest } = item;
    rest.items = item.items.map(strip);
    return rest;
}

// The seq one transition before (dir -1) or after (dir +1), or null past the ends.
function nextSeq(seg, seq, dir) {
    const next = seq + dir;
    return next >= seg.startSeq && next <= seg.headSeq ? next : null;
}

// {board: {key: value}} as of time t, or null before the blackboard's start.
function bbAt(seg, t) {
    return seg.bb.at(t);
}

// ------------------------------------------------------------------ //
// Log rows: one per retained record, optionally filtered by name
// ------------------------------------------------------------------ //
// uid -> {name, subtree} for one layout tree. A node's subtree is the nearest
// SubTree node at or above it (a SubTree card belongs to the region it opens),
// else the main tree's ID. Cached per layout: segments of one tree share it.
const NAMES = new WeakMap();

function nodeNames(layout) {
    let names = NAMES.get(layout);
    if (names) return names;
    names = new Map();
    (function walk(node, subtree) {
        if (node.is_subtree_root) subtree = node.name;
        if (node.uid !== null && node.uid !== undefined) names.set(node.uid, { name: node.name, subtree });
        for (const child of node.children || []) walk(child, subtree);
    })(layout, treeName(layout));
    NAMES.set(layout, names);
    return names;
}

function namesOf(seg, uid) {
    return nodeNames(seg.layout).get(uid) || { name: `uid ${uid}`, subtree: "" };
}

// The Log's rows: every retained record of rec, oldest first, or with a
// filter only those whose node or subtree name contains it (ignoring case).
// {count, parts: [{seg, first, seqs}]}: part rows first.. come from seg, at
// seqs (null: every seq from seg.startSeq).
function logRows(rec, filter = "") {
    const needle = filter.trim().toLowerCase();
    const parts = [];
    let count = 0;
    for (const seg of rec.segments) {
        let seqs = null;
        if (needle) {
            const match = new Set();
            for (const [uid, { name, subtree }] of nodeNames(seg.layout)) {
                if (name.toLowerCase().includes(needle) || subtree.toLowerCase().includes(needle)) {
                    match.add(uid);
                }
            }
            seqs = [];
            for (const chunk of seg.chunks) {
                for (let i = 0; i < chunk.length; i++) if (match.has(chunk.uid[i])) seqs.push(chunk.seq0 + i);
            }
        }
        const n = seqs ? seqs.length : seg.headSeq - seg.startSeq;
        if (n) parts.push({ seg, first: count, seqs });
        count += n;
    }
    return { count, parts };
}

// The status name a node had just before the record at seq: its keyframe
// value with the chunk's earlier records for it applied (≤1023).
function statusBefore(seg, seq, uid) {
    const chunk = chunkOf(seg, seq);
    const state = { [uid]: chunk.keyframe[uid] };
    for (let i = 0; i < seq - chunk.seq0; i++) {
        if (chunk.uid[i] === uid) applyTransition(state, uid, chunk.status[i]);
    }
    return decodeStatus(state[uid])[0];
}

// Row i: {seg, seq, t, uid, name, subtree, from, to}, or null past the ends.
// from and to are status names ("IDLE" for "IDLE after X").
function logRowAt(rows, i) {
    if (i < 0 || i >= rows.count) return null;
    const p = rows.parts[bisectRight(rows.parts.length, i, (j) => rows.parts[j].first) - 1];
    const seg = p.seg;
    const seq = p.seqs ? p.seqs[i - p.first] : seg.startSeq + i - p.first;
    const chunk = chunkOf(seg, seq);
    const k = seq - chunk.seq0;
    const uid = chunk.uid[k];
    const { name, subtree } = namesOf(seg, uid);
    return { seg: seg.id, seq, t: chunk.ts[k], uid, name, subtree,
             from: statusBefore(seg, seq, uid), to: STATUS_NAMES[chunk.status[k]] || "UNKNOWN" };
}

// The index of the first row at or after record (segId, seq): the row for it
// when it is one, rows.count when nothing follows.
function logRowIndex(rows, segId, seq) {
    for (const p of rows.parts) {
        if (p.seg.id < segId) continue;
        if (p.seg.id > segId) return p.first;
        const n = p.seqs ? p.seqs.length : p.seg.headSeq - p.seg.startSeq;
        const k = p.seqs ? bisectRight(n, seq - 1, (j) => p.seqs[j])
                         : Math.min(Math.max(seq - p.seg.startSeq, 0), n);
        if (k < n) return p.first + k;
    }
    return rows.count;
}

// True once eviction has dropped transitions: a whole segment (ids count up
// from 0, and only eviction removes one) or a segment's leading chunks (its
// retained start moved past its begin). The Log and the Timeline then mark
// where the kept history starts.
function historyDropped(rec) {
    const first = rec.segments[0];
    return Boolean(first) && (first.id > 0 || first.tStart > first.tBegin);
}

// Plain facts about a recording, for window.kleinDebug and the tests.
function describe(rec) {
    const last = rec.segments[rec.segments.length - 1];
    return {
        segments: rec.segments.map((s) => ({ id: s.id, layoutId: s.layoutId, startSeq: s.startSeq,
                                              headSeq: s.headSeq, tBegin: s.tBegin,
                                              tStart: s.tStart, tEnd: s.tEnd })),
        gaps: rec.gaps.map((g) => [g.tFrom, g.tTo, g.kind]),
        head: rec.head,
        tMin: rec.tMin,
        stateAtHead: last ? decodeState(last.state, last.uids) : null,
        backfillDone: rec.backfillDone,
    };
}

globalThis.KleinRecording = {
    decodeState, stateAtSeq, seqAtTime, stateAt, timeAtSeq, recordsRange, intervals, nextSeq,
    bbAt, describe, logRows, logRowAt, logRowIndex, intervalsAll, timelineMarks,
    timelineSections, historyDropped, treeName, eachNode,
    createStore: () => new Store(),
};
})();
