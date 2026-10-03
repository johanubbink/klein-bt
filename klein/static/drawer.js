// drawer.js — the bottom drawer: a toolbar with the Log and Timeline tabs, the
// recording chip and the shared filter, over a scrollable body; and the
// shared cursor's controls (Log rows, ↑/↓/Esc; app.js shows the "Viewing
// t = …" banner with its Back to live button, KleinDrawer.goLive).
//
// The drawer's height lives in the --drawer-height custom property on <html>,
// so the hint line (#watermark) rises with it in CSS alone; app.js reads the
// drawer's top edge for the camera (visibleViewport). Loaded before app.js,
// which calls KleinDrawer.showRecording and KleinDrawer.update on every
// render and moves its cursor when the drawer asks (KleinDrawer.onSeek).
// See docs/architecture.md, "The drawer".

(function () {
"use strict";

const DEFAULT_HEIGHT = 300;             // px
const MAX_FRACTION = 0.8;               // of the window's height

const drawer = document.getElementById("drawer");
const toolbar = document.getElementById("drawer-toolbar");
const chip = document.getElementById("drawer-chip");
const chipLabel = chip.querySelector(".drawer-chip-label");
const chipStats = chip.querySelector(".drawer-chip-stats");
const note = document.getElementById("drawer-note");
const tabs = [...drawer.querySelectorAll('[role="tab"]')];

// Drag a panel's edge along one axis ("x" or "y"): onMove(from, delta) with
// the panel's size() at the press and the pointer's offset since, then
// onEnd() on release. Pointer capture keeps the drag going when the pointer
// leaves the thin handle; an unmoved press (half of a double-click) changes
// nothing, so it can't expand a collapsed panel to its minimum.
function dragEdge(edge, axis, size, onMove, onEnd) {
    const coord = axis === "x" ? "clientX" : "clientY";
    edge.addEventListener("pointerdown", (event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        edge.setPointerCapture(event.pointerId);
        const start = event[coord];
        const from = size();
        const move = (e) => { if (e[coord] !== start) onMove(from, e[coord] - start); };
        const end = () => {
            edge.removeEventListener("pointermove", move);
            edge.removeEventListener("pointerup", end);
            edge.removeEventListener("pointercancel", end);
            onEnd();
        };
        edge.addEventListener("pointermove", move);
        edge.addEventListener("pointerup", end);
        edge.addEventListener("pointercancel", end);
    });
}

// A panel dragged to size on one edge and folded to a strip by its button or
// a double-click on that edge, remembered in localStorage: the drawer (below)
// and the sidebar (app.js). Its shown size goes in a custom property on <html>,
// so whatever sits beside it follows in CSS alone.
//   el, handle, button  the panel, its edge, its fold button
//   key, sizeName       the storage key, holding {[sizeName]: px, collapsed}
//   cssVar              the custom property
//   axis, grow          the drag axis ("x" or "y"), +1 or -1: the way it grows
//   initial             the size until the reader picks one
//   clamp(px), strip()  the expanded size's limits, and the collapsed size
//   labels              the button's title, [expanded, collapsed]; also its
//                       aria-label with ariaLabel
//   onResize()          after every change of the shown size
//   onFold(before)      after a fold or an unfold, with the size shown before
// Returns {shown(), collapsed()}.
function pane({ el, handle, button, key, sizeName, cssVar, axis, grow, initial, clamp, strip,
                labels, ariaLabel = false, onResize = () => {}, onFold = () => {} }) {
    let size = initial;
    let collapsed = false;
    // Storage can be missing or throw (a private window, blocked site data);
    // the panel then just starts at its default every time.
    try {
        const saved = JSON.parse(localStorage.getItem(key));
        if (saved && Number.isFinite(saved[sizeName])) size = saved[sizeName];
        if (saved) collapsed = Boolean(saved.collapsed);
    } catch (e) { /* defaults */ }

    function save() {
        try {
            localStorage.setItem(key, JSON.stringify({ [sizeName]: size, collapsed }));
        } catch (e) { /* not remembered, still works */ }
    }

    const shown = () => (collapsed ? strip() : clamp(size));

    function apply() {
        document.documentElement.style.setProperty(cssVar, `${shown()}px`);
        el.classList.toggle("collapsed", collapsed);
        const label = labels[collapsed ? 1 : 0];
        button.setAttribute("aria-expanded", String(!collapsed));
        if (ariaLabel) button.setAttribute("aria-label", label);
        button.title = label;
    }

    function toggle() {
        const before = shown();
        collapsed = !collapsed;
        apply();
        onResize();
        save();
        onFold(before);
    }

    dragEdge(handle, axis, () => (axis === "x" ? el.offsetWidth : el.offsetHeight), (from, d) => {
        collapsed = false;
        size = clamp(from + grow * d);
        apply();
        onResize();
    }, save);
    handle.addEventListener("dblclick", toggle);
    // A mouse click lets go of the focus, so R/F work right away (a key press
    // keeps it on the button).
    button.addEventListener("click", (event) => {
        toggle();
        if (event.detail) button.blur();
    });
    window.addEventListener("resize", apply);   // the limits follow the window
    apply();
    return { shown, collapsed: () => collapsed };
}

// The toolbar alone, plus the drawer's top border.
function minHeight() {
    return toolbar.offsetHeight + drawer.offsetHeight - drawer.clientHeight;
}

// Up is taller; from the toolbar alone to 80% of the window. Shown again, the
// Log and the Timeline catch up on what they skipped while folded (app.js
// requestRender).
const drawerPane = pane({
    el: drawer, handle: document.getElementById("drawer-handle"),
    button: document.getElementById("drawer-collapse"),
    key: "klein.drawer", sizeName: "height", cssVar: "--drawer-height", axis: "y", grow: -1,
    initial: DEFAULT_HEIGHT, strip: minHeight,
    clamp: (h) => Math.round(Math.max(minHeight(), Math.min(h, window.innerHeight * MAX_FRACTION))),
    labels: ["Collapse drawer", "Expand drawer"],
    onResize: () => requestRender(),
});

// The tabs share the scrolling body, so each keeps its own scroll position.
const scrollTops = {};
for (const tab of tabs) {
    tab.addEventListener("click", () => {
        for (const other of tabs) {
            const panel = document.getElementById(other.getAttribute("aria-controls"));
            if (!panel.hidden && other !== tab) scrollTops[other.id] = drawerBody.scrollTop;
            other.setAttribute("aria-selected", String(other === tab));
            panel.hidden = other !== tab;
        }
        drawerBody.scrollTop = scrollTops[tab.id] || 0;
        paintKey = null;            // the Log's rows were not kept current while hidden
        revealed = null;            // and the cursor may have moved meanwhile
        update(rec, pos);
    });
}

// ------------------------------------------------------------------ //
// The recording chip
// ------------------------------------------------------------------ //
// How much is kept, as the chip says it: "42 s", "10 min", "1 h 5 min".
function formatKept(us) {
    const seconds = us / 1e6;
    if (seconds < 60) return `${Math.floor(seconds)} s`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes} min`;
    return `${Math.floor(minutes / 60)} h` + (minutes % 60 ? ` ${minutes % 60} min` : "");
}

function formatCount(n) {
    if (n < 1000) return String(n);
    if (n < 1e4) return `${(n / 1e3).toFixed(1)}k`;
    if (n < 1e6) return `${Math.round(n / 1e3)}k`;
    return `${(n / 1e6).toFixed(1)}M`;
}

function formatBytes(bytes) {
    if (bytes < 500) return "<1 kB";        // rounds to 0 kB: there is something
    if (bytes < 1e6) return `${Math.round(bytes / 1e3)} kB`;
    if (bytes < 1e7) return `${(bytes / 1e6).toFixed(1)} MB`;
    return `${Math.round(bytes / 1e6)} MB`;
}

// What the chip says, from the browser's recording mirror and the gateway's
// `recording` state ("on", "off", "unsupported"; null before klein says).
// The window is the span actually kept, head back to the oldest record: a
// slow tree keeps up to a chunk beyond --record-buffer, a size cap less.
// An opened file (klein-bt --open) says so instead, with what it holds.
// Returns {state: "on" | "off" | "file", text, note}.
function summary(rec, support) {
    const transitions = rec ? rec.segments.reduce((n, s) => n + s.headSeq - s.startSeq, 0) : 0;
    if (rec && rec.source === "file") {
        const parts = [`file: ${rec.name}`];
        if (hasRecords(rec)) parts.push(keptSpan(rec), `${formatCount(transitions)} transitions`);
        parts.push("no robot");
        return { state: "file", text: parts.join(" · "), note: "" };
    }
    if (support === "off") {
        return { state: "off", text: "Recording off (--record-buffer 0)", note: "" };
    }
    if (support === "unsupported") {
        return { state: "off", text: "Recording needs BehaviorTree.CPP ≥ 4.3.3", note: "" };
    }
    if (!hasRecords(rec)) {
        return { state: "on", text: "Recording · waiting for the robot", note: "" };
    }
    const parts = [`Recording · last ${keptSpan(rec)}`,
                   `${formatCount(transitions)} transitions`];
    if (rec.bytes) parts.push(formatBytes(rec.bytes[0] + rec.bytes[1]));

    const notes = [];
    if (rec.capped.includes("transitions")) {
        notes.push(`transitions: last ${keptSpan(rec)} (size limit)`);
    }
    if (rec.capped.includes("blackboard")) {
        const starts = rec.segments.map((s) => s.bb.tStart).filter((t) => t !== null);
        const since = starts.length ? Math.min(...starts) : rec.head;
        notes.push(`blackboard history: last ${formatKept(rec.head - since)} (size limit)`);
    }
    return { state: "on", text: parts.join(" · "), note: notes.join(" · ") };
}

// Whether rec holds anything to show yet: a segment, and a head.
function hasRecords(rec) {
    return Boolean(rec) && rec.segments.length > 0 && rec.head !== null;
}

// The span actually kept, head back to the oldest record: "10 min".
function keptSpan(rec) {
    return formatKept(rec.head - rec.tMin);
}

// What marks where the kept history starts, once eviction dropped some (the
// Log's first line, the Timeline's left edge); null while nothing was dropped.
function droppedNote(rec) {
    return rec && rec.head !== null && R.historyDropped(rec)
        ? `Transitions older than the kept ${keptSpan(rec)} were dropped` : null;
}

function setText(el, text) {
    if (el.textContent !== text) el.textContent = text;
}

// Called on every render, so it writes only what changed. In a narrow window
// the note gives way first, then the leading "Recording · " (the red dot
// already says it), so the numbers are the last to go; hovering shows all.
function showRecording(rec, support) {
    const { state, text, note: noteText } = summary(rec, support);
    if (chip.dataset.state !== state) chip.dataset.state = state;
    const cut = text.startsWith("Recording · ") ? "Recording · ".length : 0;
    setText(chipLabel, text.slice(0, cut));
    setText(chipStats, text.slice(cut));
    if (chip.title !== text) chip.title = text;
    setText(note, noteText);
    if (note.title !== noteText) note.title = noteText;
    note.hidden = !noteText;
    showSave(rec, state, text);
}

// ------------------------------------------------------------------ //
// Save
// ------------------------------------------------------------------ //
// One click is one download: GET /log.zip, everything kept, one .btlog per
// tree run plus its .bb.jsonl blackboard (the gateway builds and names it).
// It's fetched as a blob and saved through an <a download> under the
// gateway's name, so the "Saved …" pill can say that name.
const saveButton = document.getElementById("drawer-save");

// Always shown; greyed out while klein doesn't record (the chip is grey; the
// tooltip then says why, as the chip does) or has nothing yet. The tree
// runs: segments split where the tree changes, as the gateway's
// Recording.runs().
function showSave(rec, state, chipText) {
    const empty = !hasRecords(rec);
    saveButton.disabled = state === "off" || empty;
    const runs = empty ? 0 : rec.segments.filter(
        (s, i) => i === 0 || s.layout !== rec.segments[i - 1].layout).length;
    const title = state === "off" ? chipText
        : empty ? "Nothing recorded yet"
        : `Save everything kept as one .zip · ${runs} tree run${runs === 1 ? "" : "s"}`;
    if (saveButton.title !== title) saveButton.title = title;
}

// A gateway that's gone (or answers an error) gets a "Save failed" pill.
async function saveZip() {
    let response, blob;
    try {
        response = await fetch("/log.zip");
        if (response.ok) blob = await response.blob();
    } catch (e) { /* below */ }
    if (!blob) {
        showBanner("saved", "Save failed", { kind: "error", timeout: 4000 });
        return;
    }
    const name = /filename="([^"]+)"/.exec(
        response.headers.get("Content-Disposition") || "")?.[1] || "klein.zip";
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
    showBanner("saved", `Saved ${name}`, { timeout: 4000 });
}
saveButton.addEventListener("click", saveZip);

// ------------------------------------------------------------------ //
// The Log tab and the shared cursor
// ------------------------------------------------------------------ //
// One row per retained transition, oldest first. Only the rows in view (plus
// a few either side) exist: #log-rows is as tall as all of them and a small
// pool of fixed-height rows is placed by index, so 100k rows scroll like 100.
// Once eviction dropped history, a first line (#log-dropped) says so and the
// rows sit one line lower. Past MAX_SPACER px (a few million rows: 10 min at
// the ~10k transitions/s ceiling is 6M) the spacer stops growing and the
// scroll range maps linearly onto the rows instead; below it the mapping is
// exact (scale 1).
// Clicking a row pauses the shared cursor just after that transition; the
// tree and blackboard then show that moment (app.js render()).
const R = globalThis.KleinRecording;
const C = globalThis.KleinCursor;
const ROW_HEIGHT = 20;              // px, as .log-row
const OVERSCAN = 4;                 // rows kept beyond each edge of the view
// Browsers cap an element's height (Chrome near 3.3e7 px, Firefox near
// 1.8e7 px); 500k rows of 20 px stay well under both.
const MAX_SPACER = 1e7;

const drawerBody = document.getElementById("drawer-body");
const logPanel = document.getElementById("drawer-log");
const logRowsEl = document.getElementById("log-rows");
const logEmpty = document.getElementById("log-empty");
const logDropped = document.getElementById("log-dropped");
const filterInput = document.getElementById("drawer-filter");

let seek = () => {};                // app.js: moves the shared cursor (onSeek)
let rec = null;                     // the recording and position last shown
let pos = null;
let rows = { count: 0, parts: [] }; // KleinRecording.logRows
let rowsRec = null;
let rowsKey = null;
let follow = true;                  // scrolled to the newest row: stay there while live
let wasLive = true;
let revealed = null;                // the cursor's record last scrolled into view, "seg:seq"
let paintKey = null;
let lead = 0;                       // 1 while #log-dropped takes the first line
let topRow = null;                  // {seg, seq, index} of the row last painted at the top
const pool = [];                    // row elements, reused as the view scrolls

// The list in "virtual" px: every line ROW_HEIGHT tall, as if no browser
// limit applied. The scroll range maps linearly onto it.
function virtualHeight() {
    return (rows.count + lead) * ROW_HEIGHT;
}

// Virtual px per scrolled px: 1 unless the list outgrew MAX_SPACER.
function scale() {
    const range = drawerBody.scrollHeight - drawerBody.clientHeight;
    const spacer = Math.min(virtualHeight(), MAX_SPACER);
    return range > 0 ? (range + virtualHeight() - spacer) / range : 1;
}

// (A scrollTop can read a fraction past its range, which scaled would
// leave the newest row a few px short of the bottom.)
function virtualTop() {
    const range = Math.max(0, drawerBody.scrollHeight - drawerBody.clientHeight);
    return Math.min(drawerBody.scrollTop, range) * scale();
}

function setVirtualTop(v) {
    drawerBody.scrollTop = v / scale();
}

// Robot time of day (the recording's timestamps are absolute wall-clock µs,
// as in the saved file names): "14:03:22" and `digits` (0 to 6) of the second,
// the µs grouped in threes: "14:03:22.201 030" at 6. Without `hours`, from
// the minutes on: "03:22.5".
function formatClock(t, digits, hours = true) {
    const d = new Date(Math.floor(t / 1000));
    const two = (n) => String(n).padStart(2, "0");
    const fraction = String(t % 1e6).padStart(6, "0").slice(0, digits);
    return (hours ? `${two(d.getHours())}:` : "") + `${two(d.getMinutes())}:${two(d.getSeconds())}`
        + (digits ? "." : "")
        + (fraction.length > 3 ? `${fraction.slice(0, 3)} ${fraction.slice(3)}` : fraction);
}

// A transition's time in the Log and the banner: "14:03:22.201 030".
function formatTime(t) {
    return formatClock(t, 6);
}

// The gap to the previous row: "16 µs", "2.5 ms", "500 ms", "1.20 s", "3.5 min".
function formatDelta(us) {
    if (us < 1e3) return `${us} µs`;
    if (us < 1e4) return `${(us / 1e3).toFixed(1)} ms`;
    if (us < 1e6) return `${Math.round(us / 1e3)} ms`;
    if (us < 6e7) return `${(us / 1e6).toFixed(us < 1e7 ? 2 : 1)} s`;
    return `${(us / 6e7).toFixed(1)} min`;
}

// The record the cursor is on: the one it was paused just after.
function cursorRecord() {
    return pos && !pos.live ? { seg: pos.seg, seq: pos.seq - 1 } : null;
}

function isRow(index, record) {
    const row = R.logRowAt(rows, index);
    return Boolean(row && record && row.seg === record.seg && row.seq === record.seq);
}

function makeRow() {
    const el = document.createElement("div");
    el.className = "log-row";
    el.innerHTML = '<span class="log-time"></span><span class="log-delta"></span>'
        + '<span class="log-subtree"></span><span class="log-node"></span>'
        + '<span><span class="log-from"></span><span class="log-arrow"> → </span>'
        + '<span class="log-to"></span></span>';
    const q = (s) => el.querySelector(s);
    el.cells = { time: q(".log-time"), delta: q(".log-delta"), subtree: q(".log-subtree"),
                 node: q(".log-node"), from: q(".log-from"), to: q(".log-to") };
    logRowsEl.appendChild(el);
    return el;
}

// Row `index` into a pooled element. Δ is the gap to the row above; the first
// row of a segment says instead why the recording starts again there.
function fillRow(el, index, row, prev, selected, shift) {
    const c = el.cells;
    el.hidden = false;
    el.style.transform = `translateY(${(index + lead) * ROW_HEIGHT - shift}px)`;
    el.dataset.seg = row.seg;
    el.dataset.seq = row.seq;
    c.time.textContent = formatTime(row.t);
    const boundary = prev !== null && prev.seg !== row.seg;
    let delta = "";
    if (boundary) {
        const newTree = rec.segment(row.seg).layoutId !== rec.segment(prev.seg).layoutId;
        delta = newTree ? "new tree" : "resumed";
        c.delta.title = newTree ? "The robot loaded a different tree"
                                : "Recording picked up again after an outage or an overflow";
    } else if (prev !== null) {
        delta = formatDelta(row.t - prev.t);
        c.delta.title = "";
    }
    c.delta.textContent = delta;
    c.delta.className = "log-delta" + (boundary ? " boundary"
                                                : prev !== null && row.t - prev.t < 1e3 ? " quiet" : "");
    el.classList.toggle("seg-start", boundary);
    el.classList.toggle("selected", selected !== null && row.seg === selected.seg
                                    && row.seq === selected.seq);
    c.subtree.textContent = row.subtree;
    c.subtree.title = row.subtree ? `Filter by ${row.subtree}` : "";
    c.node.textContent = row.name;
    c.node.title = `${row.name} (uid ${row.uid})`;
    c.from.textContent = row.from;
    c.from.dataset.status = row.from;
    c.to.textContent = row.to;
    c.to.dataset.status = row.to;
}

// Whether the Log's rows are on screen: its tab is shown and the drawer open.
// While they are not, the rows are not listed again on every render.
function logShown() {
    return !logPanel.hidden && !drawerPane.collapsed();
}

// Place the rows in view. Cheap when nothing moved: it runs on every render.
function paint() {
    if (!logShown()) return;
    refreshRows();
    // The cursor moved, here or in the Timeline: bring its row into view
    // (the next row, when the filter hides its own).
    const record = cursorRecord();
    const recordKey = record && `${record.seg}:${record.seq}`;
    if (recordKey !== revealed) {
        revealed = recordKey;
        if (record && rows.count) {
            follow = false;
            reveal(Math.min(R.logRowIndex(rows, record.seg, record.seq), rows.count - 1));
        }
    }
    // Rows are placed in virtual px less `shift`, which is 0 at scale 1; when
    // scaled, the rows follow every scrolled px.
    const top = virtualTop();
    const shift = Math.round(top - drawerBody.scrollTop);
    // The row at the top, which refreshRows keeps there. Read now, while
    // rows matches the store: by the next refresh, eviction may have
    // changed the segments it indexes.
    const topIndex = Math.floor(top / ROW_HEIGHT) - lead;
    if (!topRow || topRow.index !== topIndex) {
        const row = R.logRowAt(rows, topIndex);
        topRow = row && { seg: row.seg, seq: row.seq, index: topIndex };
    }
    const first = Math.max(0, Math.floor(top / ROW_HEIGHT) - lead - OVERSCAN);
    const last = Math.min(rows.count,
                          Math.ceil((top + drawerBody.clientHeight) / ROW_HEIGHT) - lead + OVERSCAN);
    const selected = cursorRecord();
    const key = `${first} ${last} ${shift} ${rowsKey} ${selected ? selected.seg + ":" + selected.seq : ""}`;
    if (key === paintKey) return;
    paintKey = key;
    logDropped.style.transform = `translateY(${-shift}px)`;
    while (pool.length < last - first) pool.push(makeRow());
    let prev = first > 0 ? R.logRowAt(rows, first - 1) : null;
    for (let i = 0; i < pool.length; i++) {
        if (first + i >= last) {
            pool[i].hidden = true;
            continue;
        }
        const row = R.logRowAt(rows, first + i);
        fillRow(pool[i], first + i, row, prev, selected, shift);
        prev = row;
    }
}

// Scroll just enough that row `index` is in view, below the sticky head;
// the first row with the dropped line above it.
function reveal(index) {
    const y = (index + lead) * ROW_HEIGHT;
    const top = virtualTop();
    const view = drawerBody.clientHeight - logRowsEl.offsetTop;
    if (y < top) setVirtualTop(index === 0 ? 0 : y);
    else if (y + ROW_HEIGHT > top + view) setVirtualTop(y + ROW_HEIGHT - view);
}

function scrollToNewest() {
    drawerBody.scrollTop = drawerBody.scrollHeight;
}

// New records, eviction, a reconnect or the filter: list the rows again.
// The store changes between frames, so painting checks this too. Scrolled
// away from the newest row, the row at the top stays there (or the next
// kept one, when eviction dropped it).
function refreshRows() {
    const dropped = droppedNote(rec);
    const key = filterInput.value + "|" + dropped + "|"
        + (rec ? rec.segments.map((s) => `${s.id}:${s.startSeq}:${s.headSeq}`).join(",") : "");
    if (rec !== rowsRec || key !== rowsKey) {
        const anchor = !follow && logShown() && topRow
            ? { ...topRow, within: virtualTop() - (topRow.index + lead) * ROW_HEIGHT } : null;
        topRow = null;
        rowsRec = rec;
        rowsKey = key;
        rows = rec ? R.logRows(rec, filterInput.value) : { count: 0, parts: [] };
        lead = dropped && rows.count ? 1 : 0;
        logDropped.hidden = !lead;
        if (lead) setText(logDropped, dropped);
        logRowsEl.style.height = `${Math.min(virtualHeight(), MAX_SPACER)}px`;
        if (anchor) {
            // Eviction dropped that row too: everything above the oldest
            // kept row went, so show the top, the dropped line included.
            const index = R.logRowIndex(rows, anchor.seg, anchor.seq);
            const row = R.logRowAt(rows, index);
            const kept = row && row.seg === anchor.seg && row.seq === anchor.seq;
            setVirtualTop(kept ? (index + lead) * ROW_HEIGHT + anchor.within : 0);
        }
        paintKey = null;
        const filter = filterInput.value.trim();
        setText(logEmpty, filter ? `No transitions match “${filter}”.`
                                 : rec ? "No transitions recorded yet." : "Nothing recorded.");
        logEmpty.hidden = rows.count > 0;
    }
}

// What the dashboard shows, from app.js's render(): the recording (null
// without one) and the cursor's position (KleinCursor.cursorPos).
function update(recording, position) {
    rec = recording || null;
    pos = rec ? position : null;
    if (pos && pos.live && !wasLive) follow = true;    // back to live, however
    wasLive = !pos || pos.live;
    if (!logShown()) return;
    refreshRows();
    if (pos && pos.live && follow) scrollToNewest();
    paint();
}

// Pause just after the record (segId, seq). The position is set here too, so
// a second key press before the next frame steps on from it.
function goTo(segId, seq) {
    follow = false;
    const clock = C.pause(segId, seq + 1);
    pos = C.cursorPos(clock, 0, rec);
    seek(clock);
}

function goLive() {
    follow = true;
    seek(C.live());
}

// One row up (-1) or down (+1) the list as filtered. From live, up takes the
// newest row; at either end the cursor stays put. False when the key does
// nothing here.
function stepRow(dir) {
    refreshRows();
    if (!rows.count) return false;
    let index;
    if (pos.live) {
        if (dir > 0) return false;
        index = rows.count - 1;
    } else {
        const record = cursorRecord();
        const at = R.logRowIndex(rows, record.seg, record.seq);
        index = dir < 0 ? at - 1 : isRow(at, record) ? at + 1 : at;
        if (index < 0 || index >= rows.count) return true;
    }
    const row = R.logRowAt(rows, index);
    reveal(index);
    goTo(row.seg, row.seq);
    return true;
}

function setFilter(text) {
    filterInput.value = text;
    filterChanged();
}

// Keep the cursor's row in view, or the newest row while live.
function filterChanged() {
    update(rec, pos);
    refreshRows();
    if (!pos) return;
    if (pos.live) {
        follow = true;
        scrollToNewest();
    } else {
        const record = cursorRecord();
        const at = R.logRowIndex(rows, record.seg, record.seq);
        if (isRow(at, record)) reveal(at);
    }
    paint();
}

logRowsEl.addEventListener("click", (event) => {
    const el = event.target.closest(".log-row");
    if (!el) return;
    if (event.target.closest(".log-subtree")) {
        if (el.cells.subtree.textContent) setFilter(el.cells.subtree.textContent);
        return;
    }
    goTo(Number(el.dataset.seg), Number(el.dataset.seq));
});
drawerBody.addEventListener("scroll", () => {
    if (!logShown()) return;
    follow = drawerBody.scrollTop + drawerBody.clientHeight >= drawerBody.scrollHeight - ROW_HEIGHT / 2;
    paint();
});
filterInput.addEventListener("input", filterChanged);

// ↑/↓ step through the rows, Esc goes back to live. Unmodified presses only,
// as for the camera keys, and not while typing (the filter) or inside the
// sidebar, whose radios and disclosures use these keys themselves. They do
// work in the drawer: that is where the rows are.
window.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
    if (!["ArrowUp", "ArrowDown", "Escape"].includes(event.key)) return;
    if (!pos || event.target.closest?.("#sidebar, input, textarea, select")) return;
    if (event.key === "Escape") {
        if (pos.live) return;
        goLive();
    } else if (!stepRow(event.key === "ArrowUp" ? -1 : 1)) {
        return;
    }
    event.preventDefault();
});

// For the tests: the rows as listed ([seg, seq] each) and the filter.
function debugRows() {
    refreshRows();
    const keys = [];
    for (let i = 0; i < rows.count; i++) {
        const row = R.logRowAt(rows, i);
        keys.push([row.seg, row.seq]);
    }
    return { count: rows.count, keys, filter: filterInput.value };
}

globalThis.KleinDrawer = {
    showRecording, summary, update, formatTime, formatClock, debugRows, goLive,
    keptSpan, droppedNote, pane,
    onSeek: (fn) => { seek = fn; },
};
})();
