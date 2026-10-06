// cursor.js — the shared cursor: which moment of a recording the dashboard shows.
//
// A clock is plain data, one of
//   {mode: "live"}                                  pinned to the head
//   {mode: "paused", seg, seq[, t]}                 at one seq of one segment
//   {mode: "playing", seg, seq, startMs[, t]}       at 1x from (seg, seq) since startMs
// and cursorPos turns it into the position to paint, {seg, seq, t, live, mode}
// (mode: "live" once a playing clock reaches the head). Live,
// scrubbing and playing differ only in the clock. The optional t is a time
// between seq's record and the next one (the Timeline's playhead dropped
// between transitions); without it the time is that of seq's record. Pure: no
// DOM, no d3, and it needs recording.js loaded first.

(function () {
"use strict";

const R = () => globalThis.KleinRecording;

function live() {
    return { mode: "live" };
}

function pause(segId, seq, t) {
    return t === undefined ? { mode: "paused", seg: segId, seq } : { mode: "paused", seg: segId, seq, t };
}

function play(fromSegId, fromSeq, nowMs, t) {
    const clock = { mode: "playing", seg: fromSegId, seq: fromSeq, startMs: nowMs };
    if (t !== undefined) clock.t = t;
    return clock;
}

function headPos(recording) {
    const last = recording.segments[recording.segments.length - 1];
    const t = recording.head !== null ? recording.head : last.tBegin;
    return { seg: last.id, seq: last.headSeq, t, live: true, mode: "live" };
}

// The time a position shows: its own, or live the later of the head and the
// newest blackboard sample.
function shownTime(pos, recording) {
    if (!pos.live) return pos.t;
    const tLast = recording.segment(pos.seg).bb.tLast;
    return tLast !== null && tLast > pos.t ? tLast : pos.t;
}

// shownTime at the head: where the Timeline and the overview end.
function headTime(recording) {
    return shownTime(headPos(recording), recording);
}

// (segment, seq) clamped to what is retained: an evicted segment's position
// moves to the oldest retained one, an evicted seq to its segment's start.
function retained(recording, segId, seq) {
    const seg = recording.segment(segId);
    if (!seg) {
        const first = recording.segments[0];
        return [first, first.startSeq];
    }
    return [seg, Math.min(Math.max(seq, seg.startSeq), seg.headSeq)];
}

// The position a clock shows at wall time nowMs, or null with nothing recorded.
function cursorPos(clock, nowMs, recording) {
    if (!recording || recording.segments.length === 0) return null;
    if (clock.mode === "live") return headPos(recording);
    const [from, fromSeq] = retained(recording, clock.seg, clock.seq);
    // A paused clock's own time holds only where eviction moved nothing. A
    // playing one keeps its time: eviction may drop where play started
    // while the moment it has reached is still kept.
    const kept = from.id === clock.seg && fromSeq === clock.seq;
    const start = clock.t !== undefined && (kept || clock.mode === "playing")
        ? clock.t : R().timeAtSeq(from, fromSeq);
    if (clock.mode === "paused") {
        return { seg: from.id, seq: fromSeq, t: start, live: false, mode: "paused" };
    }
    // Playing at 1x: robot time moves with wall time; at the head it is live.
    const t = start + Math.floor((nowMs - clock.startMs) * 1000);    // whole µs, as recorded
    if (recording.head !== null && t >= recording.head) return headPos(recording);
    let i = recording.segments.indexOf(from);
    while (i + 1 < recording.segments.length && recording.segments[i + 1].tBegin <= t) i++;
    const seg = recording.segments[i];
    let seq = t < seg.tStart ? seg.startSeq : R().seqAtTime(seg, t);
    if (seg === from) seq = Math.max(seq, fromSeq);
    return { seg: seg.id, seq, t, live: false, mode: "playing" };
}

// True when eviction has dropped the moment a clock shows: a paused clock's
// record, or the time a playing one has reached. cursorPos then shows the
// oldest kept moment instead; app.js pauses there and says so.
function evicted(clock, nowMs, recording) {
    if (clock.mode === "live" || !recording || recording.segments.length === 0) return false;
    if (clock.mode === "paused") {
        const seg = recording.segment(clock.seg);
        return !seg || clock.seq < seg.startSeq;
    }
    const pos = cursorPos(clock, nowMs, recording);
    return !pos.live && pos.t < recording.segment(pos.seg).tStart;
}

// A paused clock at robot time t (whole µs, clamped to what is kept): in the
// segment holding t, just after the last transition at or before it.
function pauseAt(recording, t) {
    t = Math.min(Math.max(Math.round(t), recording.tMin), headTime(recording));
    let seg = recording.segments[0];
    for (const s of recording.segments) if (s.tBegin <= t) seg = s;
    t = Math.max(t, seg.tStart);
    return pause(seg.id, R().seqAtTime(seg, t), t);
}

globalThis.KleinCursor = { live, pause, play, cursorPos, shownTime, headTime, pauseAt, evicted };
})();
