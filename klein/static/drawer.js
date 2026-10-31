// drawer.js — the bottom drawer: a transport row shared by both tabs (fold,
// the transport of transport.js, the overview of overview.js, the clock), a
// tab row (the Log and Timeline tabs, the shared filter, the Timeline's zoom,
// the recorder pill with Save) over a scrollable body the two tabs share
// (the Log of log.js, the Timeline of timeline.js). This module is the
// drawer itself: its pane, its tabs and which one is on screen (shownTab,
// selectedTab), the recorder pill and Save, and what is kept in words
// (keptSpan, droppedNote). Its parts ask it what is on screen; it knows none
// of them.
//
// The drawer's height lives in the --drawer-height custom property on <html>,
// so the hint line (#watermark) rises with it in CSS alone; the camera
// (tree.js, visibleViewport) reads the drawer's top edge. app.js calls
// KleinDrawer.showRecording on every render, and the drawer calls back
// through the hooks app.js connects (KleinDrawer.connect): to render, and to
// show a banner.
// See docs/architecture.md, "The drawer".

import * as R from "./recording.js";
import { blurOnClick, pane, setText, formatKept, formatCount, formatBytes } from "./ui.js";

const DEFAULT_HEIGHT = 300;             // px
const MAX_FRACTION = 0.8;               // of the window's height
const TAB_ROW_HEIGHT = 48;              // px, as #drawer-toolbar (hidden while collapsed)

const drawer = document.getElementById("drawer");
const transport = document.getElementById("drawer-transport");
const chip = document.getElementById("drawer-chip");
const chipLabel = chip.querySelector(".drawer-chip-label");
const chipStats = chip.querySelector(".drawer-chip-stats");
const note = document.getElementById("drawer-note");
const tabs = [...drawer.querySelectorAll('[role="tab"]')];
const drawerBody = document.getElementById("drawer-body");
const logPanel = document.getElementById("drawer-log");

// app.js: render at once, ask for a render, and show a banner.
let hooks = { render() {}, requestRender() {}, showBanner() {} };
const tabListeners = [];                // onTab

// ------------------------------------------------------------------ //
// The pane and the tabs
// ------------------------------------------------------------------ //
// The transport row alone, plus the drawer's top border: the collapsed strip.
function minHeight() {
    return transport.offsetHeight + drawer.offsetHeight - drawer.clientHeight;
}

// Up is taller; from both rows to 80% of the window.
const drawerPane = pane({
    el: drawer, handle: document.getElementById("drawer-handle"),
    button: document.getElementById("drawer-collapse"),
    key: "klein.drawer", sizeName: "height", cssVar: "--drawer-height", axis: "y", grow: -1,
    initial: DEFAULT_HEIGHT, strip: minHeight,
    clamp: (h) => Math.round(Math.max(minHeight() + TAB_ROW_HEIGHT,
                                      Math.min(h, window.innerHeight * MAX_FRACTION))),
    labels: ["Collapse drawer", "Expand drawer"],
    onResize: () => hooks.requestRender(),
});

// The tabs share the scrolling body, so each keeps its own scroll position.
// Only the tab shown is painted on every render, so a switch renders at
// once (app.js): the tab is painted when the click returns.
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
        for (const listener of tabListeners) listener();
        hooks.render();
    });
}

// Call fn on every tab switch, before the render that paints the tab: the
// Log's rows were not kept current while hidden.
function onTab(fn) {
    tabListeners.push(fn);
}

// The tab selected, "log" or "timeline", folded or not: the Timeline's zoom
// and \ work on its tab, the folded overview's strip being 8 windows long.
function selectedTab() {
    return logPanel.hidden ? "timeline" : "log";
}

// The tab on screen, or null while the drawer is folded. A tab not on
// screen isn't painted (the Timeline), or its rows aren't listed again (the
// Log), on every render; the overview shows the window only beside the
// Timeline.
function shownTab() {
    return drawerPane.collapsed() ? null : selectedTab();
}

// ------------------------------------------------------------------ //
// The recorder pill
// ------------------------------------------------------------------ //
// What the pill's tooltip says, from the browser's recording mirror and the
// gateway's `recording` state ("on", "off", "unsupported"; null before klein
// says).
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

// What the pill shows, shorter than its tooltip (summary's text): a label,
// "Recording" or "file: NAME", and the numbers, the span kept and its size
// ("1 min · 148 kB"). Recording off shows its reason whole, in place of the
// numbers. Returns [label, stats].
function pillText(rec, state, text) {
    if (state === "off") return ["", text];
    const label = state === "file" ? `file: ${rec.name}` : "Recording";
    if (!hasRecords(rec)) return [label, state === "file" ? "" : "waiting for the robot"];
    const stats = [keptSpan(rec)];
    if (state === "on" && rec.bytes) stats.push(formatBytes(rec.bytes[0] + rec.bytes[1]));
    return [label, stats.join(" · ")];
}

// Called on every render, so it writes only what changed. In a narrow window
// the note gives way first, then the label (the red dot already says it), so
// the numbers are the last to go; hovering shows all.
function showRecording(rec, support) {
    const { state, text, note: noteText } = summary(rec, support);
    if (chip.dataset.state !== state) chip.dataset.state = state;
    const [label, stats] = pillText(rec, state, text);
    setText(chipLabel, label);
    setText(chipStats, stats);
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

// The pill's right half, always shown; greyed out while klein doesn't record
// (the pill is grey; the tooltip then says why, as the pill's does) or has
// nothing yet. The tooltip counts the tree runs (KleinRecording.treeRuns),
// one .btlog each.
function showSave(rec, state, chipText) {
    const empty = !hasRecords(rec);
    saveButton.disabled = state === "off" || empty;
    const runs = empty ? 0 : R.treeRuns(rec).length;
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
        hooks.showBanner("saved", "Save failed", { kind: "error", timeout: 4000 });
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
    hooks.showBanner("saved", `Saved ${name}`, { timeout: 4000 });
}
saveButton.addEventListener("click", saveZip);
// A mouse click on any of the drawer's buttons lets go of the focus, so the
// keys work at once.
blurOnClick(drawer);

// app.js gives its hooks (above).
function connect(h) {
    hooks = { ...hooks, ...h };
}

export { showRecording, summary, keptSpan, droppedNote, selectedTab, shownTab, onTab, connect };
