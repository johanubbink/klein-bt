// view.js — what the dashboard shows, decided without the DOM: at each render
// the source (the recording at the cursor, or the latest frames), the clock
// after the render's rewrites, the tree, the status and the blackboard
// panel's input (frame, panel). app.js computes them and then applies them:
// the painters, the body classes, the banners. Pure: no DOM, no d3.
// See docs/architecture.md, "Rendering".

import * as R from "./recording.js";
import * as C from "./cursor.js";

// ------------------------------------------------------------------ //
// The frame: what one render shows
// ------------------------------------------------------------------ //
// Eviction dropped the moment shown: the oldest kept record (just after it,
// as clicking its Log row does). Eviction goes oldest first, so that is the
// first segment's.
function oldestKept(rec) {
    const first = rec.segments[0];
    return C.pause(first.id, Math.min(first.startSeq + 1, first.headSeq));
}

// The clock a render keeps and the position it shows, once the recording is
// backfilled (before, there is no position). A clock on an evicted moment
// moves to the oldest kept one; playing goes live once it reaches the head,
// and until then asks for the next frame.
function cursor(rec, clock, nowMs) {
    if (!rec || !rec.backfillDone) return { clock, evicted: false, playing: false, pos: null };
    const evicted = C.evicted(clock, nowMs, rec);
    if (evicted) clock = oldestKept(rec);
    const pos = C.cursorPos(clock, nowMs, rec);
    const playing = clock.mode === "playing" && pos !== null;
    if (playing && pos.live) clock = C.live();
    return { clock, evicted, playing: playing && !pos.live, pos };
}

// Whether the recording at pos is the source. An ended segment stays the
// source while the robot is away (its head is the last state klein saw);
// once klein says the robot is back but can't record, its status frames are
// shown instead.
function fromRecording(pos, seg, support) {
    return Boolean(seg) && !(pos.live && seg.tEnd !== null && support === "unsupported");
}

// What one render shows. `rec` is the recording klein streams (or null),
// `clock` the shared cursor's, `nowMs` the page's clock (performance.now()),
// `support` the robot frame's `recording` ("on", "off", "unsupported",
// "file", or null before it), and `frames` the latest frames, for when there
// is no recording to show: {tree, status, boards, track}, the `layout`,
// `status` and `blackboard` frames' data (boards null before the first) and
// the blackboard frames' track. Returns
//   clock         the clock from now on (see cursor())
//   evicted       it was moved off an evicted moment: say so
//   playing       still playing: render again on the next frame
//   pos           KleinCursor.cursorPos, or null before the backfill
//   source        "recording" or "frames"
//   seg           the cursor's segment, while the recording is the source
//   tree          the tree to draw: the segment's layout, or the latest layout frame
//   status        {uid: {status, from}}, the state to paint
//   t, mode       the time and mode the blackboard is shown at: the frames
//                 are always live at their newest sample
//   viewingPast   the cursor isn't live (body.viewing-past)
//   noBoards      an opened file without a .bb.jsonl sidecar: there is no
//                 blackboard at any moment (body.no-blackboard)
function frame({ rec, clock, nowMs, frames, support }) {
    const shown = cursor(rec, clock, nowMs);
    const { pos } = shown;
    const seg = pos && rec.segment(pos.seg);
    if (!fromRecording(pos, seg, support)) {
        return { ...shown, source: "frames", seg: null, tree: frames.tree, status: frames.status,
                 t: frames.track.tLast, mode: "live", viewingPast: false, noBoards: false };
    }
    return {
        ...shown, source: "recording", seg, tree: seg.layout,
        status: R.decodeState(R.stateAtSeq(seg, pos.seq), seg.uids),
        t: C.shownTime(pos, rec), mode: pos.mode, viewingPast: !pos.live,
        noBoards: rec.source === "file" && seg.bb.tStart === null,
    };
}

// What the blackboard panel says when it lists no board, by why: a sample
// holding none, or nothing to show at this moment.
const NO_BOARDS = {
    none: "This robot reports no blackboards.",
    file: "No blackboard in this file.",
    dropped: "Blackboard history from this moment was dropped.",
    early: "No blackboard sample yet at this moment.",
    waiting: "Waiting for values…",
};

// Why a frame without boards shows none, or null to leave the panel as it
// is. A past moment shows none, since what is shown belongs to another
// moment: before the segment's kept blackboard, dropped by eviction (which
// cuts the blackboard at the exact cutoff, transitions by whole chunks), or
// a moment before its first sample. Live keeps what is shown, or waits for
// values when the panel last showed the past.
function noBoardsReason(view, wasLive) {
    if (view.noBoards) return "file";
    if (view.viewingPast) return view.seg.bbDropped ? "dropped" : "early";
    return wasLive ? null : "waiting";
}

// The blackboard panel's input at a frame: renderBlackboards' arguments
// {boards, opts: {track, t, mode, empty}}, or null to leave the panel as it
// is. `wasLive`: the panel last showed live values. Live takes the newest
// sample, even one stamped a moment after the last drain's head; there is
// none before a segment's first sample.
function panel(view, frames, wasLive) {
    const recording = view.source === "recording";
    const boards = recording ? R.bbAt(view.seg, view.pos.live ? Infinity : view.pos.t)
                             : frames.boards;
    const reason = boards ? "none" : noBoardsReason(view, wasLive);
    if (reason === null) return null;
    const track = recording ? view.seg.bb : frames.track;
    return { boards: boards || {},
             opts: { track, t: view.t, mode: view.mode, empty: NO_BOARDS[reason] } };
}

export { frame, panel };
