// window.js — the Timeline's window: the stretch of the recording it shows,
// (t1 - span, t1] in robot µs. The Timeline draws its axis and bars for it,
// and the overview shows it as its box and its scrollbar's thumb; both move
// it as the reader asks (set, zoomBy, step, fit), and redraw on onMove.
// app.js calls KleinWindow.sync on every render, whichever tab is shown,
// before either paints. Pure: no DOM, no d3, and it needs recording.js and
// cursor.js loaded first.
// See docs/architecture.md, "The drawer".

(function () {
"use strict";

const C = () => globalThis.KleinCursor;

// Window lengths the − and + buttons step through, in µs: 200 µs (a burst of
// transitions a few µs apart) to 1 h (beyond the default 10 min kept).
const LADDER = [200, 500, 1e3, 2e3, 5e3, 1e4, 2e4, 5e4, 1e5, 2e5, 5e5, 1e6, 2e6, 5e6, 1e7,
                2e7, 3e7, 6e7, 1.2e8, 3e8, 6e8, 1.2e9, 1.8e9, 3.6e9];
const DEFAULT_SPAN = 3e7;           // 30 s: a few CrossDoor laps

// A window of its own: the dashboard's is globalThis.KleinWindow, the tests
// make fresh ones.
function create() {
    let rec = null;                 // what sync() was last given
    let pos = null;
    let span = DEFAULT_SPAN;        // the window: (t1 - span, t1], robot µs
    let t1 = null;
    let follow = true;              // while live, the window's right edge is the head
    let lastPos = null;             // the cursor's position at the last sync
    let held = false;               // a scrub: the window holds still
    const listeners = [];           // onMove's

    // The window's right end live (KleinCursor.headTime).
    function head() {
        return C().headTime(rec);
    }

    // A window length within LADDER's.
    function onLadder(length) {
        return Math.min(Math.max(length, LADDER[0]), LADDER[LADDER.length - 1]);
    }

    // Keep the window on the recording: never past the head, and never
    // before the oldest record once there is more than a window of it. A
    // window longer than everything kept ends at the head, so now is at the
    // right edge from the first second; the time before the oldest record is
    // empty, its start edge marked (timeline.js).
    function clamp() {
        const end = head();
        span = onLadder(span);
        t1 = Math.max(Math.min(t1, end), Math.min(end, rec.tMin + span));
    }

    // The rules of every sync: back to live, the window follows the head
    // again; live and following, it ends at the head; a cursor moved
    // elsewhere (a Log row, a step, playing) is brought into view, centred.
    // A scrub holds the window still, even one longer than the recording
    // (which ends at the head, so moves on with it): the first sync after it
    // catches up.
    function rules() {
        if (pos.live && lastPos && !lastPos.live) follow = true;
        if (t1 === null || (pos.live && follow)) t1 = head();
        const moved = !lastPos || lastPos.seg !== pos.seg || lastPos.seq !== pos.seq
            || lastPos.t !== pos.t;
        lastPos = pos;
        if (moved && !pos.live && !held && (pos.t < t1 - span || pos.t > t1)) t1 = pos.t + span / 2;
        if (!held) clamp();
    }

    // {t0, t1, span, follow}, or null without a recording to show.
    function get() {
        return pos ? { t0: t1 - span, t1, span, follow } : null;
    }

    // What the dashboard shows, from app.js's render(): the recording (null
    // without one) and the cursor's position.
    function sync(recording, position) {
        rec = recording || null;
        pos = rec ? position : null;
        if (pos) rules();
    }

    function notify() {
        for (const fn of listeners) fn();
    }

    // After the reader moved the window: follow the head again only if they
    // brought it back there.
    function readerMoved() {
        clamp();
        follow = pos.live && t1 >= head();
        notify();
    }

    // Move the window to (right - length, right], as the reader's own move
    // (a lane drag, a sideways wheel, the overview's box and scrollbar).
    // Clamped as always.
    function set(right, length = span) {
        if (!pos) return;
        t1 = right;
        span = length;
        readerMoved();
    }

    // Change the window's length, keeping the time at anchor where it is
    // on screen; within LADDER's limits.
    function zoomTo(length, anchor) {
        const next = onLadder(length);
        if (!pos || next === span) return;
        const fraction = (anchor - (t1 - span)) / span;
        span = next;
        t1 = anchor + (1 - fraction) * span;
        readerMoved();
    }

    // Ctrl+wheel, over the Timeline and the overview: deltaY scales the
    // window around anchor, held inside it.
    function zoomBy(deltaY, anchor) {
        if (!pos) return;
        zoomTo(span * Math.exp(deltaY * 0.002), Math.min(Math.max(anchor, t1 - span), t1));
    }

    // The − (dir -1) and + (dir 1) buttons step along LADDER, anchored on the
    // head while following live, else on the cursor when it is in view, else
    // on the middle.
    function step(dir) {
        const next = dir < 0 ? [...LADDER].reverse().find((s) => s < span * 0.999)
                             : LADDER.find((s) => s > span * 1.001);
        if (!pos || next === undefined) return;
        const inView = pos.t >= t1 - span && pos.t <= t1;
        zoomTo(next, pos.live && follow ? t1 : inView ? pos.t : t1 - span / 2);
    }

    // Zoom to fit (⤢, and \ in drawer.js): the window is everything kept,
    // the oldest record to the head, within LADDER's limits; it follows the
    // head again while live.
    function fit() {
        if (!pos) return;
        t1 = head();
        span = t1 - rec.tMin;
        readerMoved();
    }

    // A scrub holds the window still while it lasts. Releasing the
    // Timeline's playhead just lets go; releasing the overview's strip
    // reveals the cursor too.
    function hold(on) {
        held = on;
    }

    // Count the cursor as moved, so one outside the window is brought into
    // view, centred; the views redraw.
    function reveal() {
        lastPos = null;
        if (pos) rules();
        notify();
    }

    // fn() after each of the reader's moves (not after sync, which the views
    // follow by painting on every render).
    function onMove(fn) {
        listeners.push(fn);
    }

    return { sync, get, set, zoomBy, step, fit, hold, reveal, onMove };
}

globalThis.KleinWindow = Object.assign(create(), { create });
})();
