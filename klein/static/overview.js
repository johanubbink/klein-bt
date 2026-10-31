// overview.js — the drawer's overview, in its transport row: a strip of the
// recording at a fixed zoom, 8 of the Timeline's windows long (what is kept,
// transitions per column, gaps, tree-run starts, the cursor's line and, on
// the Timeline tab, the window's box), over a scrollbar for the whole
// recording that shows while the pointer is over it. Press the strip to move
// the cursor and drag to scrub; drag the box or the scrollbar's thumb, or
// turn the wheel, to pan; Ctrl+wheel zooms. The window is KleinWindow's
// (window.js). See docs/architecture.md, "The drawer".

import * as R from "./recording.js";
import * as C from "./cursor.js";
import * as W from "./window.js";
import * as KleinDrawer from "./drawer.js";
import { drag, setStyle, formatClock } from "./ui.js";

const ZOOM = 8;                     // the strip is this many of the Timeline's windows
const COLUMN = 2;                   // px: an activity column, about
const LOW = 2;                      // px: the least height of a column with anything in it
const THUMB = 18;                   // px: the scrollbar's thumb is drawn at least this wide
const SLOP = 3;                     // px: a press on the box that moves less is a click

const drawer = document.getElementById("drawer");
const ov = document.getElementById("overview");
const strip = document.getElementById("ov-strip");
const keptEl = document.getElementById("ov-kept");
const path = document.querySelector("#ov-activity path");
const stripMarks = document.getElementById("ov-strip-marks");
const box = document.getElementById("ov-box");
const line = document.getElementById("ov-cursor");
const scrollbar = document.getElementById("ov-scrollbar");
const marksEl = document.getElementById("ov-marks");
const thumb = document.getElementById("ov-thumb");
const tick = document.getElementById("ov-tick");
const tip = document.getElementById("ov-tip");

let rec = null;                     // what update() was last given
let pos = null;
let seek = () => {};                // app.js: moves the shared cursor (onSeek)
let shown = null;                   // the layout as last painted
let held = null;                    // scrubbing the strip: its start, held still
const painted = new Map();          // the activity and each set of marks: what drawn for
// The strip's size (the scrollbar is as wide), kept current by an observer
// rather than read (a layout) on every render.
let width = Math.max(1, strip.clientWidth);
let height = strip.clientHeight;
new ResizeObserver(() => {
    width = Math.max(1, strip.clientWidth);
    height = strip.clientHeight;
    paint();
}).observe(strip);

function clamp(v, lo, hi) {
    return Math.min(Math.max(v, lo), hi);
}

function px(v) {
    return `${v.toFixed(2)}px`;
}

// False when part was last drawn for this key (nothing to redraw).
function changed(part, key) {
    key = key.join(" ");
    if (painted.get(part) === key) return false;
    painted.set(part, key);
    return true;
}

function seekTime(t) {
    seek(C.pauseAt(rec, t));
}

// Where the strip and the scrollbar lie, or null with nothing to show.
// The strip: S µs (8 windows) from m0, k px per µs, placed by the window
// shown, or without one by the cursor (a window of no length). While the
// recording fits in the strip, the strip ends at the head; longer, the
// window's place on the strip is its place in the recording (VS Code's
// minimap), so the box sweeps all of it from one end of the strip to the
// other. Scrubbing holds it still. The scrollbar (bar): the whole
// recording, wa to the head at k px per µs (on the Timeline tab, while that
// is shorter than the window, the window's length back from the head:
// nothing to scroll, so no thumb).
function layout() {
    const current = W.get();        // given what update() was: null without a recording
    if (!current) return null;
    const win = KleinDrawer.shownTab() === "timeline" ? current : null;     // the box
    const tMin = rec.tMin, head = C.headTime(rec), length = head - tMin;
    const S = ZOOM * current.span;
    const at = C.shownTime(pos, rec);
    const t0 = win ? win.t0 : at, w = win ? win.span : 0;
    const m0 = held !== null ? held
        : length <= S ? head - S : tMin + clamp((t0 - tMin) / (length - w), 0, 1) * (length - S);
    const wa = win ? Math.min(tMin, head - win.span) : tMin;
    return { win, tMin, head, length, S, m0, k: width / S, at,
             bar: { wa, wb: head, k: width / Math.max(1, head - wa) },
             scrolls: Boolean(win) && length > win.span };
}

// Gaps and tree-run starts over [t0, t1] at k px per µs, into el; redrawn
// only when that, the width or what is kept changes.
function paintMarks(el, part, t0, t1, k) {
    if (!changed(part, [t0, t1, k, rec.gaps.length, rec.segments.map((seg) => seg.id).join(",")])) return;
    const x = (t) => (t - t0) * k;
    const out = [];
    for (const gap of R.gapsIn(rec, t0, t1)) {
        const left = Math.max(0, x(gap.tFrom)), right = Math.min(width, x(gap.tTo));
        out.push(`<i class="ov-gap" style="left:${px(left)};width:${px(Math.max(1, right - left))}"></i>`);
    }
    for (const run of R.treeRuns(rec).slice(1)) {
        const t = rec.segment(run[0]).tBegin;
        if (t >= t0 && t <= t1) out.push(`<i class="ov-run" style="left:${px(x(t) - 0.5)}"></i>`);
    }
    el.innerHTML = out.join("");
}

// The transitions per column of the strip over [a, b], as one path. The
// columns lie on a grid of whole µs, so a column keeps its count as the
// strip scrolls, and their heights are log-scaled against the busiest column
// of everything kept (KleinRecording.busiestColumn), so they hold still as
// it scrolls too.
function paintActivity(s, a, b) {
    const d = Math.max(1, Math.round(COLUMN / s.k));       // µs per column
    const last = rec.segments[rec.segments.length - 1];
    if (!changed("activity", [s.m0, s.k, a, b, height, rec.segments.length, last.headSeq, rec.tMin])) return;
    const scale = Math.log1p(R.busiestColumn(rec, d));
    const w = (d * s.k).toFixed(2);
    // Columns c0 … (c0 + n - 1), column c being (c·d, (c + 1)·d].
    const c0 = Math.ceil(a / d) - 1, n = Math.max(0, Math.ceil(b / d) - c0);
    const counts = R.countIn(rec, Array.from({ length: n + 1 }, (_, i) => (c0 + i) * d));
    const out = [];
    counts.forEach((count, i) => {
        if (!count) return;
        const h = Math.max(LOW, Math.min(1, Math.log1p(count) / scale) * (height - 2));
        out.push(`M${(((c0 + i) * d - s.m0) * s.k).toFixed(2)} ${(height - h).toFixed(2)}h${w}v${h.toFixed(2)}h-${w}z`);
    });
    path.setAttribute("d", out.join(""));
}

// The scrollbar: its marks, the window's thumb and the cursor's tick.
function paintScrollbar(s) {
    const { wa, wb, k } = s.bar;
    paintMarks(marksEl, "bar", wa, wb, k);
    thumb.hidden = !s.scrolls;
    if (s.scrolls) {
        const l = (s.win.t0 - wa) * k, r = (s.win.t1 - wa) * k, w = Math.max(THUMB, r - l);
        setStyle(thumb, "left", px(clamp((l + r - w) / 2, 0, width - w)));
        setStyle(thumb, "width", px(w));
    }
    tick.hidden = false;
    setStyle(tick, "left", px(clamp((s.at - wa) * k - 1, 0, width - 2)));
    tick.classList.toggle("live", pos.live);
    const title = s.win ? "The whole recording · drag the thumb to pan, click to bring the window there"
                        : "The whole recording · click or drag to move the cursor";
    if (scrollbar.title !== title) scrollbar.title = title;
}

// Painted folded too: the transport row is the collapsed strip.
function paint() {
    const s = shown = layout();
    ov.classList.toggle("empty", !s);
    if (!s) {
        box.hidden = line.hidden = thumb.hidden = tick.hidden = tip.hidden = true;
        setStyle(keptEl, "width", "0px");
        if (painted.size) {
            path.setAttribute("d", "");
            stripMarks.textContent = marksEl.textContent = "";
            painted.clear();
        }
        return;
    }
    const x = (t) => (t - s.m0) * s.k;
    // What is kept, and in it the activity, the gaps and the run starts.
    const a = Math.max(s.m0, s.tMin), b = Math.min(s.m0 + s.S, s.head);
    setStyle(keptEl, "left", px(x(a)));
    setStyle(keptEl, "width", px(Math.max(0, x(b) - x(a))));
    paintActivity(s, a, b);
    paintMarks(stripMarks, "strip", s.m0, s.m0 + s.S, s.k);
    // The window's box: an eighth of the strip, whatever the zoom.
    box.hidden = !s.win;
    if (s.win) {
        setStyle(box, "left", px(x(s.win.t0)));
        setStyle(box, "width", px(s.win.span * s.k));
    }
    // The cursor's line, while it is on the strip (live: its right end).
    line.hidden = x(s.at) < -1 || x(s.at) > width + 1;
    setStyle(line, "left", px(clamp(x(s.at), 0, width)));
    line.classList.toggle("live", pos.live);
    paintScrollbar(s);
}

// What the dashboard shows, from app.js's render(), after KleinWindow.sync:
// the recording (null without one) and the cursor's position.
function update(recording, position) {
    rec = recording || null;
    pos = rec ? position : null;
    paint();
}

// How long before the head: "now", "42 s ago", "3 min 5 s ago".
function ago(seconds) {
    if (seconds < 0.5) return "now";
    const s = Math.round(seconds), m = Math.floor(s / 60);
    return s < 60 ? `${s} s ago` : s % 60 ? `${m} min ${s % 60} s ago` : `${m} min ago`;
}

// The time under the pointer, above the strip, over what is kept:
// "13:05:12.4 · 42 s ago" (an opened file: the time only).
function hover(event) {
    const r = strip.getBoundingClientRect();
    const t = shown ? shown.m0 + (event.clientX - r.left) / shown.k : null;
    tip.hidden = !shown || event.clientY > r.bottom + 1 || t < shown.tMin || t > shown.head;
    if (tip.hidden) return;
    tip.textContent = formatClock(Math.round(t), 1)
        + (rec.source === "file" ? "" : ` · ${ago((shown.head - t) / 1e6)}`);
    const half = tip.offsetWidth / 2;
    tip.style.left = px(r.left - drawer.getBoundingClientRect().left
                        + clamp(event.clientX - r.left, half, width - half));
}

// A press on the strip puts the cursor there and a drag scrubs, the strip
// and the Timeline's window held still until the release (then the window
// is brought to the cursor). On the box, a drag pans the window with the box
// under the pointer, and a click without a drag puts the cursor there. On
// the scrollbar, the thumb pans the window and a press beside it brings the
// window there, then drags on; with no window (the Log, or folded) a press
// there moves the cursor instead.
ov.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();             // no focus from the mouse: R and F keep working
    const s = layout();
    if (!s) return;
    const left = strip.getBoundingClientRect().left;
    const at = (e) => clamp(e.clientX - left, 0, width);
    const dx = (e) => e.clientX - event.clientX;
    let move, end = () => {}, panning = true;
    if (event.target.closest("#ov-scrollbar")) {
        const { wa, k } = s.bar;
        if (!s.win) {
            panning = false;
            move = (e) => seekTime(wa + at(e) / k);
            move(event);
        } else {
            if (!s.scrolls) return;     // the recording fits the window: nothing to scroll
            if (!event.target.closest("#ov-thumb")) {
                W.set(wa + at(event) / k + s.win.span / 2);     // centred there
            }
            const t1 = W.get().t1;
            move = (e) => W.set(t1 + dx(e) / k);
        }
    } else if (s.win && event.target.closest("#ov-box")) {
        // Δt per px that keeps the box under the pointer: the strip's scale
        // while the recording fits in it, else the proportional rule's.
        const perPx = s.length > s.S ? (s.length - s.win.span) / Math.max(1, width - s.win.span * s.k)
                                     : 1 / s.k;
        let panned = false;
        move = (e) => {
            if (!panned && Math.abs(dx(e)) < SLOP) return;
            panned = true;
            W.set(s.win.t1 + dx(e) * perPx);
        };
        end = (e) => { if (!panned && e.type === "pointerup") seekTime(s.m0 + at(event) / s.k); };
    } else {
        panning = false;
        held = s.m0;
        W.hold(true);
        move = (e) => seekTime(held + at(e) / s.k);
        move(event);
        end = () => {
            held = null;
            W.hold(false);
            W.reveal();                     // repaints (onMove)
        };
    }
    ov.classList.add("dragging");
    ov.classList.toggle("panning", panning);
    drag(ov, event, move, (e) => {
        end(e);
        ov.classList.remove("dragging", "panning");
    });
});
ov.addEventListener("pointermove", hover);
ov.addEventListener("pointerleave", () => { tip.hidden = true; });

// With a window (the Timeline tab), the wheel pans it, as the Timeline's
// sideways wheel does; Ctrl+wheel zooms around the time under the pointer,
// held inside the window.
ov.addEventListener("wheel", (event) => {
    const s = layout();
    if (!s || !s.win) return;
    event.preventDefault();
    if (event.ctrlKey) {
        const t = s.m0 + (event.clientX - strip.getBoundingClientRect().left) / s.k;
        W.zoomBy(event.deltaY, t);
    } else {
        const d = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
        W.set(s.win.t1 + d * s.win.span / width);
    }
}, { passive: false });

// A moved window moves the box and the thumb at once.
W.onMove(paint);

// For the tests: the strip on screen and where it lies (m0, S; tMin and tMax
// the recording's ends as painted), the box (px from the strip's left, null
// when not shown), and the span the scrollbar covers (it is as wide).
function debug() {
    const s = shown;
    if (!s) return null;
    return {
        left: strip.getBoundingClientRect().left, width, m0: s.m0, S: s.S, tMin: s.tMin, tMax: s.head,
        box: s.win && { x: (s.win.t0 - s.m0) * s.k, w: s.win.span * s.k },
        bar: { t0: s.bar.wa, t1: s.bar.wb },
    };
}

// app.js gives its seek (above).
function onSeek(fn) {
    seek = fn;
}

export { update, debug, onSeek };
