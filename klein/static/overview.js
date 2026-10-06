// overview.js — the whole kept recording on one track in the drawer's
// transport row: played part, gaps, tree-run starts, the cursor's knob and
// (Timeline tab) a thumb for the Timeline's window. Press/drag seeks; drag the
// thumb to pan, its edges to zoom. See docs/architecture.md, "The drawer".

(function () {
"use strict";

const R = globalThis.KleinRecording;
const T = globalThis.KleinTimeline;
const C = globalThis.KleinCursor;

const NARROW = 16;                  // px: a thumb this narrow pans only (no edges)
const MIN_THUMB = 6;                // px: the thumb drawn at least this wide (centred)
const SLOP = 3;                     // px: a press on the thumb that moves less is a click

const drawer = document.getElementById("drawer");
const ov = document.getElementById("overview");
const track = document.getElementById("ov-track");
const played = document.getElementById("ov-played");
const marksEl = document.getElementById("ov-marks");
const thumb = document.getElementById("ov-thumb");
const knob = document.getElementById("ov-knob");
const timelinePanel = document.getElementById("drawer-timeline");

let rec = null;                     // what update() was last given
let pos = null;
let marksKey = null;
let marksSpan = null;               // {tMin, tMax, width} the marks were last drawn for
let shown = null;                   // and the knob and the thumb
// The track's width and left edge in #overview, kept current by an observer
// rather than read (a layout) on every render.
let trackWidth = Math.max(1, track.clientWidth);
let trackLeft = track.offsetLeft;
new ResizeObserver(() => {
    trackWidth = Math.max(1, track.clientWidth);
    trackLeft = track.offsetLeft;
    paint();
}).observe(track);

function ready() {
    return Boolean(rec && pos && rec.segments.length);
}

// The track: µs to px from its left edge, over [tMin, head].
function scale() {
    const tMin = rec.tMin, tMax = C.headTime(rec);
    return { tMin, tMax, width: trackWidth, k: trackWidth / Math.max(1, tMax - tMin) };
}

function timeAt(clientX) {
    const { tMin, k } = scale();
    return tMin + (clientX - track.getBoundingClientRect().left) / k;
}

function setStyle(el, name, value) {
    if (el.style[name] !== value) el.style[name] = value;
}

// Gaps and run starts: rebuilt when the span, the width or what is kept changes.
function paintMarks(s) {
    const key = [s.tMin, s.tMax, s.width, rec.gaps.length,
                 rec.segments.map((seg) => seg.id).join(",")].join(" ");
    if (key === marksKey) return;
    marksKey = key;
    marksSpan = s;
    const x0 = trackLeft;
    const x = (t) => (t - s.tMin) * s.k;
    const out = [];
    for (const gap of R.gapsIn(rec, s.tMin, s.tMax)) {
        const left = Math.max(0, x(gap.tFrom)), right = Math.min(s.width, x(gap.tTo));
        out.push(`<i class="ov-gap" style="left:${(x0 + left).toFixed(2)}px;`
                 + `width:${Math.max(1, right - left).toFixed(2)}px"></i>`);
    }
    for (const run of R.treeRuns(rec).slice(1)) {
        const t = rec.segment(run[0]).tBegin;
        if (t >= s.tMin && t <= s.tMax) out.push(`<i class="ov-run" style="left:${(x0 + x(t) - 1).toFixed(2)}px"></i>`);
    }
    marksEl.innerHTML = out.join("");
}

// Painted folded too: the transport row is the collapsed strip.
function paint() {
    const ok = ready();
    ov.classList.toggle("empty", !ok);
    if (!ok) {
        knob.hidden = thumb.hidden = true;
        setStyle(played, "width", "0px");
        if (marksKey !== null) {
            marksEl.textContent = "";
            marksKey = null;
            marksSpan = null;
        }
        return;
    }
    const s = scale();
    shown = s;
    const x0 = trackLeft;
    const at = Math.min(Math.max(C.shownTime(pos, rec), s.tMin), s.tMax);
    const x = (at - s.tMin) * s.k;
    knob.hidden = false;
    setStyle(knob, "left", `${(x0 + x).toFixed(2)}px`);
    knob.classList.toggle("live", pos.live);
    setStyle(played, "width", `${x.toFixed(2)}px`);

    // The Timeline's window, on its tab: the part of it inside the track.
    // Folded, no window is shown (nor kept current).
    const win = timelinePanel.hidden || drawer.classList.contains("collapsed") ? null
        : T.getWindow();
    thumb.hidden = !win;
    if (win) {
        const left = (Math.max(win.t0, s.tMin) - s.tMin) * s.k;
        const right = (Math.min(win.t1, s.tMax) - s.tMin) * s.k;
        const width = Math.max(MIN_THUMB, right - left);
        setStyle(thumb, "left", `${(x0 + (left + right - width) / 2).toFixed(2)}px`);
        setStyle(thumb, "width", `${width.toFixed(2)}px`);
        thumb.classList.toggle("narrow", right - left < NARROW);
    }
    paintMarks(s);
}

// What the dashboard shows, from app.js's render(), after the Timeline's
// update (so the thumb is its window as just painted): the recording the
// Timeline was given (null without one) and the cursor's position.
function update(recording, position) {
    rec = recording || null;
    pos = rec ? position : null;
    paint();
}

// Press on the track: the cursor goes there, and follows a drag. On the
// thumb: a drag pans the Timeline's window (a click without one still goes
// there); on its edge, zooms it with the other edge held, between the
// Timeline's shortest and longest window and inside the recording.
ov.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();             // no focus from the mouse: R and F keep working
    if (!ready()) return;
    ov.setPointerCapture(event.pointerId);
    const win = thumb.hidden ? null : T.getWindow();
    const edge = win && event.target.closest(".ov-edge");
    let move, click = () => {};
    if (edge) {
        const left = edge.dataset.edge === "left";
        // A window longer than the recording ends past the head: hold the head.
        const fixed = left ? Math.min(win.t1, C.headTime(rec)) : win.t0;
        const clamp = (span, most) => Math.max(win.min, Math.min(span, win.max, most));
        move = (e) => {
            const t = timeAt(e.clientX);
            if (left) {
                T.setWindow(fixed, clamp(fixed - t, fixed - rec.tMin));
            } else {
                const span = clamp(t - fixed, C.headTime(rec) - fixed);
                T.setWindow(fixed + span, span);
            }
        };
    } else if (win && event.target.closest("#ov-thumb")) {
        const k = scale().k;
        let panned = false;
        move = (e) => {
            if (!panned && Math.abs(e.clientX - event.clientX) < SLOP) return;
            panned = true;
            T.setWindow(win.t1 + (e.clientX - event.clientX) / k, win.span);
        };
        click = (e) => { if (!panned) T.seekTime(timeAt(e.clientX)); };
    } else {
        move = (e) => T.seekTime(timeAt(e.clientX));
        move(event);
    }
    const end = (e) => {
        if (e.type === "pointerup") click(e);
        ov.removeEventListener("pointermove", move);
        ov.removeEventListener("pointerup", end);
        ov.removeEventListener("pointercancel", end);
    };
    ov.addEventListener("pointermove", move);
    ov.addEventListener("pointerup", end);
    ov.addEventListener("pointercancel", end);
});

// The thumb shows on the Timeline tab only; a moved window moves it at once.
for (const tab of drawer.querySelectorAll('[role="tab"]')) tab.addEventListener("click", paint);
T.connect({ windowMoved: paint });

// For the tests: the track's left edge on screen, the span and width the
// knob and the thumb were last placed for, and those the marks were drawn for.
function debug() {
    if (!ready() || !shown) return null;
    const span = ({ tMin, tMax, width }) => ({ tMin, tMax, width });
    return { left: track.getBoundingClientRect().left, ...span(shown),
             marks: marksSpan && span(marksSpan) };
}

globalThis.KleinOverview = { update, debug };
})();
