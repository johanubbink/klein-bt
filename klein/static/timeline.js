// timeline.js — the drawer's Timeline tab: one row per node of the tree on the
// canvas, in tree order, with a section per subtree, and each node's RUNNING
// spans as bars along a shared time axis; the shared cursor is its playhead.
//
// The rows follow the tree's own fold (a chevron folds the node on the canvas
// too, Alt-click folds every other subtree) and the drawer's filter. The bars
// are computed from the browser's recording mirror (KleinRecording.intervalsAll
// and timelineMarks), so dragging the playhead, stepping and playing only move
// the cursor (app.js seek hook) and never ask the gateway. The window shown
// is KleinWindow's (window.js). app.js calls KleinTimeline.update on every
// render and connects its hooks (KleinTimeline.connect).
// See docs/architecture.md, "The drawer".

import * as R from "./recording.js";
import * as C from "./cursor.js";
import * as W from "./window.js";
import * as KleinDrawer from "./drawer.js";

const TICK_SPACING = 100;           // px between axis labels, about
const MARK = 3;                     // px: an outcome cap or a single mark
// Following live, the pause between two rebuilds of the bars, in multiples
// of the last rebuild's script and layout time (painting it costs about half
// as much again): see paintBars.
const BUSY_FACTOR = 8;
// The most records a window's bars are drawn for, so that rebuilding them
// stays well inside a frame's budget (docs/testing.md). A wider window shows
// "Zoom in to see bars" instead; its axis, bands and playhead still work.
const MAX_WINDOW_RECORDS = 20_000;

const drawerBody = document.getElementById("drawer-body");
const panel = document.getElementById("drawer-timeline");
const filterInput = document.getElementById("drawer-filter");
// The window's length and its zoom, in the drawer's tab row.
const windowLabel = document.getElementById("tl-window");
const zoomGroup = document.getElementById("tl-zoom");
const zoomIn = document.getElementById("tl-zoom-in");
const zoomOut = document.getElementById("tl-zoom-out");
const fitButton = document.getElementById("tl-fit");
const ruler = document.getElementById("tl-ruler");
const rulerTrack = document.getElementById("tl-ruler-track");
const grip = document.getElementById("tl-grip");
const lanes = document.getElementById("tl-lanes");
const bandsEl = document.getElementById("tl-bands");
const rowsEl = document.getElementById("tl-rows");
const playhead = document.getElementById("tl-playhead");
const empty = document.getElementById("tl-empty");
const droppedNoteEl = document.getElementById("tl-dropped-note");
const tooManyEl = document.getElementById("tl-too-many");

// app.js: move the shared cursor, fold the canvas, and the cards' glyphs.
let hooks = { seek() {}, toggleFold() {}, foldOthers() {}, glyphFor: () => "" };
let rec = null;                     // what update() was last given
let pos = null;
let root = null;                    // the canvas's d3 hierarchy (its fold state)
let rowsKey = null;
let barsKey = null;
let nextBarsAt = 0;                 // following live: no rebuild before this (performance.now)
let barsTimer = null;
let barsT1 = null;                  // the window's right edge the bars were last drawn for
let rowEls = [];                    // [{el, track, item}] in tree order

// The axis: px from the track's left edge to µs and back, for the window shown.
function trackWidth() {
    return Math.max(1, rulerTrack.clientWidth);
}

function timeAt(clientX) {
    const w = W.get();
    return w.t0 + (clientX - rulerTrack.getBoundingClientRect().left) * w.span / trackWidth();
}

// The window's length: "10 s", "500 ms", "1.25 s", "200 µs", "10 min".
function formatWindow(us) {
    const short = (v) => String(Number(v.toPrecision(3)));
    if (us < 1e3) return `${Math.round(us)} µs`;
    if (us < 1e6) return `${short(us / 1e3)} ms`;
    if (us < 6e7) return `${short(us / 1e6)} s`;
    return `${short(us / 6e7)} min`;
}

// An axis label at robot time t for ticks `step` µs apart: the time of day,
// "14:03:22" for whole seconds, else "03:22.5" with as many decimals as the
// step needs, the µs grouped as in the Log ("03:22.201 030").
function formatTick(t, step) {
    if (step >= 1e6) return KleinDrawer.formatClock(t, 0);
    return KleinDrawer.formatClock(t, Math.min(6, Math.ceil(-Math.log10(step / 1e6) - 1e-9)), false);
}

// The 1-2-5 step giving labels about TICK_SPACING px apart, for a window
// span µs long.
function tickStep(span) {
    const raw = span * TICK_SPACING / trackWidth();
    const power = 10 ** Math.floor(Math.log10(raw));
    for (const m of [1, 2, 5, 10]) if (m * power >= raw) return Math.max(1, m * power);
    return raw;
}

// ------------------------------------------------------------------ //
// Rows: the tree in tree order, a section per subtree
// ------------------------------------------------------------------ //
function foldedIds() {
    const out = new Set();
    R.eachNode(root, (node) => {
        if (node._children) out.add(node.data.id);
    });
    return out;
}

function makeRow(item, header) {
    const el = document.createElement("div");
    el.className = "tl-row" + (header ? " tl-header" : "") + (item.context ? " context" : "");
    el.dataset.id = item.id;
    if (item.uid !== null) el.dataset.uid = item.uid;
    el.dataset.tint = item.tint;
    const label = el.appendChild(document.createElement("span"));
    label.className = "tl-label" + (item.category ? ` cat-${item.category}` : "");
    label.style.paddingLeft = `${16 + item.depth * 14}px`;
    label.title = `${item.type} "${item.name}"` + (item.uid !== null ? ` (uid ${item.uid})` : "");
    if (item.expandable) {
        const chevron = label.appendChild(document.createElement("button"));
        chevron.type = "button";
        chevron.className = "tl-chevron";
        chevron.textContent = item.folded ? "▸" : "▾";
        chevron.setAttribute("aria-expanded", String(!item.folded));
        chevron.setAttribute("aria-label", `${item.folded ? "Unfold" : "Fold"} ${item.label}`);
        chevron.title = (item.folded ? "Unfold" : "Fold") + " · Alt-click: fold every other subtree";
        chevron.addEventListener("click", (event) => {
            event.stopPropagation();
            (event.altKey ? hooks.foldOthers : hooks.toggleFold)(item.id);
        });
    } else {
        label.appendChild(document.createElement("span")).className = "tl-chevron";
    }
    const glyph = label.appendChild(document.createElement("span"));
    glyph.className = "tl-glyph";
    glyph.textContent = hooks.glyphFor(item);
    label.appendChild(document.createElement("span")).className = "tl-name";
    label.lastChild.textContent = item.label;
    const meta = header ? `${item.count} nodes` : item.folded ? `+${item.hidden}` : "";
    if (meta) {
        const m = label.appendChild(document.createElement("span"));
        m.className = "tl-meta";
        m.textContent = meta;
    }
    const track = el.appendChild(document.createElement("div"));
    track.className = "tl-track";
    rowEls.push({ el, track, item });
    return el;
}

function buildSection(item, parent) {
    const section = parent.appendChild(document.createElement("div"));
    section.className = "tl-section";
    section.appendChild(makeRow(item, true));
    for (const child of item.items) {
        if (child.section) buildSection(child, section);
        else section.appendChild(makeRow(child, false));
    }
}

// A new tree, a fold or the filter: build the rows again. Cheap to call on
// every paint; it rebuilds only when one of those changed.
function refreshRows() {
    const folded = root ? foldedIds() : new Set();
    const key = (root ? root.data.id : "") + "|" + [...folded].join(",") + "|" + filterInput.value;
    if (key === rowsKey) return;
    rowsKey = key;
    barsKey = null;
    rowEls = [];
    rowsEl.textContent = "";
    const sections = root ? R.timelineSections(root.data, folded, filterInput.value) : null;
    if (sections) buildSection(sections, rowsEl);
}

// ------------------------------------------------------------------ //
// Bars
// ------------------------------------------------------------------ //
// The window's marks per uid of the tree shown, over every segment of that
// tree in the window (a resume after an outage is the same tree), and bands
// for the rest: gaps, and stretches when the robot ran another tree.
function collect(seg, t0, t1, withBars) {
    const per = new Map();
    const bands = [];
    const add = (uid, marks) => {
        const into = per.get(uid);
        if (!into) per.set(uid, marks);
        else for (const k of Object.keys(marks)) into[k].push(...marks[k]);
    };
    rec.segments.forEach((s, i) => {
        const next = rec.segments[i + 1];
        const end = s.tEnd !== null ? s.tEnd : next ? next.tBegin : C.headTime(rec);
        if (end < t0 || s.tBegin > t1) return;
        if (!R.sameTree(s, seg)) {
            const name = R.treeName(s.layout);
            const same = name === R.treeName(seg.layout);
            bands.push({ from: s.tBegin, to: end, kind: "tree", label: name,
                         title: same ? `Earlier run of ${name}` : `The robot ran another tree: ${name}` });
            return;
        }
        if (!withBars) return;
        const all = R.intervalsAll(s, s.uids, t0, Math.min(t1, end));
        for (const uid of s.uids) add(uid, R.timelineMarks(all[uid]));
    });
    for (const gap of R.gapsIn(rec, t0, t1)) {
        bands.push({ from: gap.tFrom, to: gap.tTo, kind: "gap", label: gap.kind });
    }
    return { per, bands };
}

// One row's track as HTML: the node's bars, caps and marks, a header's
// failure marks from inside its section, a folded row's activity below it.
// Shapes landing on the same pixel are drawn once.
function trackHtml(item, header, per, k, t0) {
    const drawn = new Set();
    const out = [];
    const x = (t) => (t - t0) * k;
    const put = (cls, left, width) => {
        const key = `${cls}|${Math.round(left)}|${Math.round(width)}`;
        if (drawn.has(key)) return;
        drawn.add(key);
        out.push(`<i class="${cls}" style="left:${left.toFixed(2)}px`
                 + (width === null ? "" : `;width:${width.toFixed(2)}px`) + '"></i>');
    };
    const own = item.uid !== null && per.get(item.uid);
    if (own) {
        for (const bar of own.bars) {
            const x0 = x(bar.t0), x1 = x(bar.t1);
            put("tl-bar", x0, Math.max(2, x1 - x0 - (bar.end ? MARK : 0)));
            if (bar.end) put(`tl-cap s-${bar.end}`, x1 - MARK, null);
        }
        for (const mark of own.marks) put(`tl-mark s-${mark.status}`, x(mark.t) - MARK / 2, null);
    }
    if (item.folded) {
        for (const uid of item.inner) {
            for (const t of (per.get(uid) || { changes: [] }).changes) put("tl-sub kid", x(t) - 1, null);
        }
    }
    if (header) {
        for (const uid of item.inner) {
            for (const t of (per.get(uid) || { failures: [] }).failures) put("tl-sub s-3", x(t) - 1, null);
        }
    }
    return out.join("");
}

function paintBars() {
    const seg = rec.segment(pos.seg);
    const width = trackWidth();
    const { t0, t1, span, follow } = W.get();
    // Only what lies inside the window: paused on a still window, new
    // records past its right edge change nothing on screen.
    const dropped = KleinDrawer.droppedNote(rec);
    const key = [t0, span, width, rowsKey, pos.seg,
                 rec.segments.map((s) => `${s.id}:${R.seqAtTime(s, t0)}:${R.seqAtTime(s, t1)}:${s.tEnd}`).join(","),
                 rec.gaps.length, rec.tMin, dropped].join(" ");
    if (key === barsKey) return;
    // Following live, the window moves on every drain, and a window holding
    // a busy tree's records (100k+ at a few thousand per second) costs more
    // than a frame to rebuild and paint. So leave BUSY_FACTOR times the last
    // rebuild's cost between two rebuilds: at the MAX_WINDOW_RECORDS limit
    // (~40 ms of script and layout) the bars and the axis then step about
    // every 0.3 s instead of taking the whole main thread. A quiet tree's
    // rebuild takes well under a millisecond, so it still moves with every
    // drain (10 a second). The timer repaints once the wait is over, in case
    // nothing else asks.
    const started = performance.now();
    if (pos.live && follow && started < nextBarsAt) {
        if (!barsTimer) {
            barsTimer = setTimeout(() => { barsTimer = null; paint(); }, nextBarsAt - started);
        }
        return;
    }
    barsKey = key;
    barsT1 = t1;
    const k = width / span;
    const inWindow = R.countIn(rec, [t0, t1])[0];
    const tooMany = inWindow > MAX_WINDOW_RECORDS;
    tooManyEl.hidden = !tooMany;
    if (tooMany) {
        tooManyEl.textContent = `Zoom in to see bars: ${inWindow.toLocaleString("en-US")} `
            + `transitions in this window, bars are drawn for up to `
            + `${MAX_WINDOW_RECORDS.toLocaleString("en-US")}`;
    }
    const { per, bands } = collect(seg, t0, t1, !tooMany);
    for (const { track, item, el } of rowEls) {
        track.innerHTML = trackHtml(item, el.classList.contains("tl-header"), per, k, t0);
    }
    bandsEl.innerHTML = bands.map((b) => {
        const left = Math.max(0, (b.from - t0) * k);
        const right = Math.min(width, (b.to - t0) * k);
        return `<div class="tl-band ${b.kind}" style="left:${left.toFixed(2)}px;`
            + `width:${Math.max(1, right - left).toFixed(2)}px"><span></span></div>`;
    }).join("");
    bands.forEach((b, i) => {      // labels as text, never as HTML
        bandsEl.children[i].firstChild.textContent = b.kind === "gap" ? b.label : `tree: ${b.label}`;
        bandsEl.children[i].title = b.kind === "gap" ? `Not recorded (${b.label})` : b.title;
    });
    // Where the recording starts, when that is inside the window (a window
    // longer than the recording, or panned back to its oldest record): a
    // dashed edge over the rows, its note in the name column beside the
    // axis. Once eviction dropped history, the note says what was dropped.
    const edge = rec.tMin >= t0 && rec.tMin <= t1;
    const note = dropped || "Recording starts";
    if (edge) {
        const mark = bandsEl.appendChild(document.createElement("div"));
        mark.className = "tl-dropped";
        mark.style.left = `${((rec.tMin - t0) * k).toFixed(2)}px`;
        mark.title = note;
    }
    droppedNoteEl.hidden = !edge;
    droppedNoteEl.textContent = edge ? `⇤ ${note}` : "";
    droppedNoteEl.title = edge ? note : "";         // cut to fit the name column
    // The axis.
    const step = tickStep(span);
    const ticks = [];
    for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) {
        ticks.push(`<span class="tl-tick" style="left:${((t - t0) * k).toFixed(2)}px"></span>`);
    }
    rulerTrack.innerHTML = ticks.join("");
    let t = Math.ceil(t0 / step) * step;
    for (const el of rulerTrack.children) {
        el.textContent = formatTick(t, step);
        t += step;
    }
    void lanes.offsetHeight;        // lay the new bars out now, so that counts too
    const now = performance.now();
    nextBarsAt = now + BUSY_FACTOR * (now - started);
}

// ------------------------------------------------------------------ //
// Painting
// ------------------------------------------------------------------ //
// The Timeline is shown on its tab with the drawer open. Nothing is painted
// otherwise; showing it asks for a render, which paints again.
function shown() {
    return KleinDrawer.shownTab() === "timeline";
}

function paint() {
    if (!shown()) return;
    refreshRows();
    const ready = Boolean(pos && rowEls.length);
    lanes.hidden = !ready;
    empty.hidden = ready;
    if (!ready) {
        const filter = filterInput.value.trim();
        empty.textContent = filter && pos ? `No nodes match “${filter}”.`
                                          : rec ? "No transitions recorded yet." : "Nothing recorded.";
        return;
    }
    paintBars();

    const w = W.get();
    const x = (C.shownTime(pos, rec) - w.t0) * trackWidth() / w.span;     // live: at the head
    const visible = x >= -1 && x <= trackWidth() + 1;
    playhead.hidden = grip.hidden = !visible;
    playhead.style.left = grip.style.left = `${x.toFixed(2)}px`;
    playhead.classList.toggle("live", pos.live);
}

// What the dashboard shows, from app.js's render(), after KleinWindow.sync:
// the recording (null without one), the cursor's position and the canvas's
// hierarchy.
function update(recording, position, hierarchy) {
    rec = recording || null;
    pos = rec ? position : null;
    root = hierarchy || null;
    paintZoom();
    paint();
}

// The zoom (− window +), on the Timeline's tab with something recorded: the
// window's length, whatever the bars show.
function paintZoom() {
    zoomGroup.hidden = KleinDrawer.selectedTab() !== "timeline" || !pos;
    const label = pos ? formatWindow(W.get().span) : "";
    if (windowLabel.textContent !== label) windowLabel.textContent = label;
}

function seekTime(t) {
    hooks.seek(C.pauseAt(rec, t));
}

// The ruler: press to put the playhead there, drag to scrub.
ruler.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || !pos) return;
    event.preventDefault();
    W.hold(true);                   // the window holds still under the playhead
    seekTime(timeAt(event.clientX));
    KleinDrawer.drag(ruler, event, (e) => seekTime(timeAt(e.clientX)), () => W.hold(false));
});

// The lanes: drag to pan the time axis; a click without a drag puts the
// playhead there. The name column only folds (its chevrons).
lanes.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || !pos || event.target.closest(".tl-label")) return;
    const startX = event.clientX;
    const startT1 = W.get().t1;
    let panned = false;
    KleinDrawer.drag(lanes, event, (e) => {
        if (!panned && Math.abs(e.clientX - startX) < 3) return;
        panned = true;
        W.set(startT1 - (e.clientX - startX) * W.get().span / trackWidth());
    }, (e) => {
        if (!panned && e.type === "pointerup"
            && e.clientX >= rulerTrack.getBoundingClientRect().left) seekTime(timeAt(e.clientX));
    });
});

// Ctrl+wheel zooms around the pointer; a sideways wheel (or Shift+wheel)
// pans. A plain wheel scrolls the rows.
panel.addEventListener("wheel", (event) => {
    if (!pos) return;
    if (event.ctrlKey) {
        event.preventDefault();
        W.zoomBy(event.deltaY, timeAt(event.clientX));
    } else if (event.shiftKey || Math.abs(event.deltaX) > Math.abs(event.deltaY)) {
        event.preventDefault();
        const w = W.get();
        W.set(w.t1 + (event.shiftKey ? event.deltaY : event.deltaX) * w.span / trackWidth());
    }
}, { passive: false });

zoomIn.addEventListener("click", () => W.step(-1));
zoomOut.addEventListener("click", () => W.step(1));
fitButton.addEventListener("click", () => W.fit());
// The reader's own move: rebuild the bars at once.
W.onMove(() => {
    nextBarsAt = 0;
    paintZoom();
    paint();
});
filterInput.addEventListener("input", paint);
window.addEventListener("resize", () => {
    barsKey = null;
    paint();
});

// F with the drawer open: scroll the first of these nodes' rows (the running
// frontier, app.js) into view, under the sticky axis and section header.
function revealRows(ids) {
    if (!shown()) return;
    refreshRows();
    const wanted = new Set(ids);
    const row = rowEls.find((r) => wanted.has(r.item.id));
    if (!row) return;
    const top = row.el.getBoundingClientRect().top - drawerBody.getBoundingClientRect().top
        + drawerBody.scrollTop;
    // Under its section's sticky header, unless it is that header.
    const header = row.el.classList.contains("tl-header") ? 0
        : row.el.parentElement.firstElementChild.offsetHeight;
    drawerBody.scrollTop = Math.max(0, top - panel.querySelector(".tl-head").offsetHeight - header);
}

// For the tests: the window (KleinWindow.get), the axis's place on screen,
// the cursor, and the right edge the bars were last drawn for.
function debug() {
    return { ...W.get(), left: rulerTrack.getBoundingClientRect().left, width: trackWidth(), pos, barsT1 };
}

// app.js gives its hooks (above).
function connect(h) {
    hooks = { ...hooks, ...h };
}

export { update, revealRows, debug, connect };
