// log.js — the drawer's Log tab: a virtual list of the recording's
// transitions under the shared filter (below). It follows the newest row
// while live, keeps the row at its top across a new listing, and keeps the
// shared cursor's row selected and in view. The transport steps through its
// rows from either tab (step, setFollow) and moves the cursor for a row's
// click (onPick); app.js calls KleinLog.update on every render. It paints
// only while its tab is on screen (KleinDrawer.shownTab).
// See docs/architecture.md, "The drawer".

import * as R from "./recording.js";
import * as V from "./view.js";
import * as KleinDrawer from "./drawer.js";
import { setText, formatTime, formatDelta } from "./ui.js";

// One row per retained transition, oldest first. Only the rows in view (plus
// a few either side) exist: #log-rows is as tall as all of them and a small
// pool of fixed-height rows is placed by index, so 100k rows scroll like 100.
// Once eviction dropped history, a first line (#log-dropped) says so and the
// rows sit one line lower. Past MAX_SPACER px (a few million rows: 10 min at
// the ~10k transitions/s ceiling is 6M) the spacer stops growing and the
// scroll range maps linearly onto the rows instead; below it the mapping is
// exact (scale 1).
// Clicking a row pauses the shared cursor just after that transition
// (through the transport: onPick); the tree and blackboard then show that
// moment (app.js render()).
const ROW_HEIGHT = 20;              // px, as .log-row
const OVERSCAN = 4;                 // rows kept beyond each edge of the view
// Browsers cap an element's height (Chrome near 3.3e7 px, Firefox near
// 1.8e7 px); 500k rows of 20 px stay well under both.
const MAX_SPACER = 1e7;

const drawerBody = document.getElementById("drawer-body");
const logRowsEl = document.getElementById("log-rows");
const logEmpty = document.getElementById("log-empty");
const logDropped = document.getElementById("log-dropped");
const filterInput = document.getElementById("drawer-filter");

let rec = null;                     // the recording and position last shown
let pos = null;
let pick = () => {};                // transport.js: pause just after a row (onPick)
let rows = R.logRows(null);         // KleinRecording.logRows
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

// The record a cursor at `position` is on: the one it was paused just after.
function recordOf(position) {
    return position && !position.live ? { seg: position.seg, seq: position.seq - 1 } : null;
}

// The record the cursor shown is on.
function cursorRecord() {
    return recordOf(pos);
}

function isRow(index, record) {
    const row = rows.at(index);
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
        const newTree = !R.sameTree(rec.segment(row.seg), rec.segment(prev.seg));
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

function logShown() {
    return KleinDrawer.shownTab() === "log";
}

// The cursor moved, here or in the Timeline: bring its row into view (the
// next row, when the filter hides its own).
function revealCursor() {
    const record = cursorRecord();
    const recordKey = record && `${record.seg}:${record.seq}`;
    if (recordKey === revealed) return;
    revealed = recordKey;
    if (record && rows.count) {
        follow = false;
        reveal(Math.min(rows.indexOf(record.seg, record.seq), rows.count - 1));
    }
}

// The row at index, at the top: refreshRows keeps it there. Noted while
// painting, while rows matches the store: by the next refresh, eviction may
// have changed the segments it indexes.
function noteTopRow(index) {
    if (topRow && topRow.index === index) return;
    const row = rows.at(index);
    topRow = row && { seg: row.seg, seq: row.seq, index };
}

// Rows first..last-1 into the pool, placed `shift` px up; the rest of the
// pool hidden.
function placeRows(first, last, shift, selected) {
    logDropped.style.transform = `translateY(${-shift}px)`;
    while (pool.length < last - first) pool.push(makeRow());
    let prev = first > 0 ? rows.at(first - 1) : null;
    for (let i = 0; i < pool.length; i++) {
        if (first + i >= last) {
            pool[i].hidden = true;
            continue;
        }
        const row = rows.at(first + i);
        fillRow(pool[i], first + i, row, prev, selected, shift);
        prev = row;
    }
}

// Place the rows in view. Cheap when nothing moved: it runs on every render.
function paint() {
    if (!logShown()) return;
    refreshRows();
    revealCursor();
    // Rows are placed in virtual px less `shift`, which is 0 at scale 1; when
    // scaled, the rows follow every scrolled px.
    const top = virtualTop();
    const shift = Math.round(top - drawerBody.scrollTop);
    const topIndex = Math.floor(top / ROW_HEIGHT) - lead;
    noteTopRow(topIndex);
    const first = Math.max(0, topIndex - OVERSCAN);
    const last = Math.min(rows.count,
                          Math.ceil((top + drawerBody.clientHeight) / ROW_HEIGHT) - lead + OVERSCAN);
    const selected = cursorRecord();
    const key = `${first} ${last} ${shift} ${rowsKey} ${selected ? selected.seg + ":" + selected.seq : ""}`;
    if (key === paintKey) return;
    paintKey = key;
    placeRows(first, last, shift, selected);
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

// The row at the top to keep there when the rows are listed again, with
// the px scrolled into it ({seg, seq, index, within}): scrolled away from the
// newest row on the Log's tab; else null.
function topAnchor() {
    if (follow || !logShown() || !topRow) return null;
    return { ...topRow, within: virtualTop() - (topRow.index + lead) * ROW_HEIGHT };
}

// What the Log says when it lists no row.
function paintEmpty() {
    const filter = filterInput.value.trim();
    setText(logEmpty, filter ? `No transitions match “${filter}”.`
                             : rec ? "No transitions recorded yet." : "Nothing recorded.");
    logEmpty.hidden = rows.count > 0;
}

// New records, eviction, a reconnect or the filter: list the rows again.
// The store changes between frames, so painting checks this too. Scrolled
// away from the newest row, the row at the top stays there, or the list
// shows its top when that row went (KleinView.anchorAfter).
function refreshRows() {
    const dropped = KleinDrawer.droppedNote(rec);
    const key = filterInput.value + "|" + dropped + "|"
        + (rec ? rec.segments.map((s) => `${s.id}:${s.startSeq}:${s.headSeq}`).join(",") : "");
    if (rec === rowsRec && key === rowsKey) return;
    const anchor = topAnchor();
    topRow = null;
    rowsRec = rec;
    rowsKey = key;
    rows = R.logRows(rec, filterInput.value);
    lead = dropped && rows.count ? 1 : 0;
    logDropped.hidden = !lead;
    if (lead) setText(logDropped, dropped);
    logRowsEl.style.height = `${Math.min(virtualHeight(), MAX_SPACER)}px`;
    if (anchor) {
        const index = V.anchorAfter(rows, anchor);
        setVirtualTop(index === null ? 0 : (index + lead) * ROW_HEIGHT + anchor.within);
    }
    paintKey = null;
    paintEmpty();
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

// The row one up (-1) or down (+1) the Log as filtered from a cursor at
// `position`, whichever tab is shown: the transport's |◀ ▶| and ↑/↓. From
// live, up takes the newest row. On the Log's tab it is scrolled into view.
// Returns it ({seg, seq, …}); null at either end, where the cursor stays put;
// false when the step does nothing here.
function step(position, dir) {
    refreshRows();
    if (!rows.count) return false;
    let index;
    if (position.live) {
        if (dir > 0) return false;
        index = rows.count - 1;
    } else {
        const record = recordOf(position);
        const at = rows.indexOf(record.seg, record.seq);
        index = dir < 0 ? at - 1 : isRow(at, record) ? at + 1 : at;
        if (index < 0 || index >= rows.count) return null;
    }
    if (logShown()) reveal(index);
    return rows.at(index);
}

// Follow the newest row while live (true), or stay where the reader is: the
// transport's moves, going live or to a row.
function setFollow(on) {
    follow = on;
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
        const at = rows.indexOf(record.seg, record.seq);
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
    pick(Number(el.dataset.seg), Number(el.dataset.seq));
});
drawerBody.addEventListener("scroll", () => {
    if (!logShown()) return;
    follow = drawerBody.scrollTop + drawerBody.clientHeight >= drawerBody.scrollHeight - ROW_HEIGHT / 2;
    paint();
});
filterInput.addEventListener("input", filterChanged);
KleinDrawer.onTab(() => {
    paintKey = null;            // the rows were not kept current while hidden
    revealed = null;            // and the cursor may have moved meanwhile
});

// For the tests: the rows as listed ([seg, seq] each) and the filter.
function debugRows() {
    refreshRows();
    const keys = [];
    for (let i = 0; i < rows.count; i++) {
        const row = rows.at(i);
        keys.push([row.seg, row.seq]);
    }
    return { count: rows.count, keys, filter: filterInput.value };
}

// transport.js gives the move a row's click makes: fn(segId, seq) pauses the
// shared cursor just after that record.
function onPick(fn) {
    pick = fn;
}

export { update, step, setFollow, onPick, debugRows };
