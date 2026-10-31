// view.js — what the dashboard shows, decided without the DOM: at each render
// the source (the recording at the cursor, or the latest frames), the clock
// after the render's rewrites, the tree, the status and the blackboard
// panel's input (frame, panel); each blackboard row's change marks
// (rowMarks); what the Timeline's window shows (timelineWindow); and which
// row the Log keeps at its top (anchorAfter). app.js, blackboard.js,
// timeline.js and log.js compute them and then apply them: the painters, the
// body classes, the banners, the rows' classes, the lanes, the scroll
// position.
// Pure: no DOM, no d3.
// See docs/architecture.md, "Rendering" and "The drawer".

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

// The blackboard panel's input at a frame: KleinBlackboard.render's arguments
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

// ------------------------------------------------------------------ //
// Row marks: a blackboard key's change, as its row shows it
// ------------------------------------------------------------------ //
// A change's marks, in µs like the recording's times.
const BB_SAMPLE = 500e3;        // fresh: changed within one 2 Hz sample
const BB_STREAMING = 4;         // streaming: this many changes in KleinRecording.BB_RECENT
const BB_FADE = 1.8e6;          // the fade's length (styles.css, --fade)

// What a key's change (KleinRecording.bbChange) is at time t. Streaming
// (BB_STREAMING changes in BB_RECENT) wins over news (fresh: changed under
// one sample ago); `over`: a fade it started would have run out.
function changeKind(change, t) {
    const streaming = change.recent >= BB_STREAMING;
    const age = change.tChange === null ? Infinity : t - change.tChange;
    const news = age < BB_SAMPLE && !streaming;
    return { streaming, news, over: age >= BB_FADE };
}

// What a paint in `mode` may do, whatever the row. Moving (live or playing),
// news fades once (`animate`), unless the panel was last painted in another
// mode or from another source (`primed` false: a switch only primes the
// rows, so a jump never animates) or motion is reduced. Paused, or with
// reduced motion (`still`), news is marked statically and nothing fades.
function paintRule(mode, primed, reducedMotion) {
    const moving = mode === "live" || mode === "playing";
    return { animate: moving && primed && !reducedMotion, still: !moving || reducedMotion };
}

// One row's marks at a paint: from its key's `change` (bbChange at the time
// shown, `t`), the paint's `mode`, `primed` and `reducedMotion` (paintRule),
// whether the row is `shown` (its board open, the sidebar expanded), and
// `seen`, the change the row was last painted for (undefined until its first
// paint, which is not a change: the key is just being seen). Returns
//   streaming   the steady streaming mark (bb-stream, a `~`)
//   marked      the static mark (bb-mark); its writers hold the pulse
//   fire        a change seen as it passes: its writers' pulse fades once,
//               shown or not
//   fade        the row's one-shot fade (bb-fresh): "restart" it, "clear" it
//               (also one a hidden row never played: an animation doesn't
//               run under display: none, and would play on showing, long
//               after the change), or "keep" whatever runs
function rowMarks({ change, t, mode, primed, reducedMotion, shown, seen }) {
    const rule = paintRule(mode, primed, reducedMotion);
    const { streaming, news, over } = changeKind(change, t);
    const fire = rule.animate && news && seen !== undefined && change.tChange !== seen;
    let fade = "keep";
    if (fire && shown) fade = "restart";
    else if (rule.still || !shown || streaming || over) fade = "clear";
    return { streaming, marked: rule.still && news, fire, fade };
}

// ------------------------------------------------------------------ //
// The Timeline's window: what its lanes show from t0 to t1
// ------------------------------------------------------------------ //
// Where segment i of rec ends: its end, else where the next one begins,
// else the time shown at the head (KleinCursor.headTime).
function segmentEnd(rec, i) {
    const s = rec.segments[i], next = rec.segments[i + 1];
    return s.tEnd !== null ? s.tEnd : next ? next.tBegin : C.headTime(rec);
}

// Segment s's stretch, to `end`, as a band over the rows of seg's tree: the
// robot ran another tree, or another run of the same one (a new layout).
function treeBand(s, seg, end) {
    const name = R.treeName(s.layout);
    const title = name === R.treeName(seg.layout) ? `Earlier run of ${name}`
                                                   : `The robot ran another tree: ${name}`;
    return { from: s.tBegin, to: end, kind: "tree", label: `tree: ${name}`, title };
}

// Segment s's marks from t0 to t1 (KleinRecording.timelineMarks), joined
// uid by uid to those per holds already.
function addMarks(per, s, t0, t1) {
    const all = R.intervalsAll(s, s.uids, t0, t1);
    for (const uid of s.uids) {
        const marks = R.timelineMarks(all[uid]);
        const into = per.get(uid);
        if (!into) per.set(uid, marks);
        else for (const k of Object.keys(marks)) into[k].push(...marks[k]);
    }
}

// The Timeline's window from t0 to t1, drawn for the tree of segment `seg`
// (the cursor's): what its lanes show, for timeline.js to paint. Returns
//   count     the kept transitions in it (t0 < ts <= t1, as countIn)
//   tooMany   more than maxRecords: no bars ("Zoom in to see bars"), so
//             rebuilding them stays inside a frame's budget
//   per       Map uid -> timelineMarks over every segment of that tree in
//             the window (a resume after an outage is the same tree),
//             oldest first; empty when tooMany
//   bands     [{from, to, kind, label, title}] over every row, the bands
//             the window overlaps: kind "tree" where the robot ran another
//             tree (or another run of it), then kind "gap" for each gap
//             (label its kind: not recorded)
//   edge      where the recording starts (rec.tMin) when that is inside
//             the window, else null
function timelineWindow(rec, seg, t0, t1, maxRecords) {
    const count = R.countIn(rec, [t0, t1])[0];
    const tooMany = count > maxRecords;
    const per = new Map();
    const bands = [];
    rec.segments.forEach((s, i) => {
        const end = segmentEnd(rec, i);
        if (end < t0 || s.tBegin > t1) return;
        if (!R.sameTree(s, seg)) bands.push(treeBand(s, seg, end));
        else if (!tooMany) addMarks(per, s, t0, Math.min(t1, end));
    });
    for (const gap of R.gapsIn(rec, t0, t1)) {
        bands.push({ from: gap.tFrom, to: gap.tTo, kind: "gap", label: gap.kind,
                     title: `Not recorded (${gap.kind})` });
    }
    const edge = rec.tMin >= t0 && rec.tMin <= t1 ? rec.tMin : null;
    return { count, tooMany, per, bands, edge };
}

// ------------------------------------------------------------------ //
// The Log's anchor: the row at the top, across a new listing
// ------------------------------------------------------------------ //
// Where the row at the top of the Log, topRow {seg, seq}, is once the rows
// (KleinRecording.logRows) are listed again after new records, eviction or
// a new filter: its index, which the Log keeps at the top, or null when it
// is no longer listed. The Log then shows its top, the dropped line
// included: eviction goes oldest first, so a dropped row took every row
// above it.
function anchorAfter(rows, topRow) {
    const index = rows.indexOf(topRow.seg, topRow.seq);
    const row = rows.at(index);
    return row && row.seg === topRow.seg && row.seq === topRow.seq ? index : null;
}

export { frame, panel, rowMarks, timelineWindow, anchorAfter };
