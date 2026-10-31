// transport.js — the drawer's transport row: |◀ ▶ ▶|, Live (Go live in the
// past; End / Go to end for an opened file) and the clock, and the keys,
// each doing what its button does. It makes the shared cursor's moves from
// the drawer: a step to the Log's previous or next row as filtered, from
// either tab (KleinLog.step), play and pause, Home, back to live, and a Log
// row's click (KleinLog.onPick). app.js calls KleinTransport.update on every
// render and connects its seek (KleinTransport.connect).
// See docs/architecture.md, "The drawer".

import * as C from "./cursor.js";
import * as W from "./window.js";
import * as KleinDrawer from "./drawer.js";
import * as KleinLog from "./log.js";
import { setText, setAttr, formatTime } from "./ui.js";

// app.js: move the shared cursor.
let hooks = { seek() {} };
let rec = null;                     // the recording last shown
let pos = null;                     // the position last shown, or moved to here since

// What the dashboard shows, from app.js's render(): the recording (null
// without one) and the cursor's position (KleinCursor.cursorPos).
function update(recording, position) {
    rec = recording || null;
    pos = rec ? position : null;
    paintTransport();
}

// ------------------------------------------------------------------ //
// The shared cursor's moves
// ------------------------------------------------------------------ //
// Move the shared cursor. The position is set here too, so a second key
// press before the next frame acts from there.
function setClock(clock) {
    pos = C.cursorPos(clock, performance.now(), rec);
    hooks.seek(clock);
}

// Pause just after the record (segId, seq): a step, or a Log row's click.
function goTo(segId, seq) {
    KleinLog.setFollow(false);
    setClock(C.pause(segId, seq + 1));
}

function goLive() {
    KleinLog.setFollow(true);
    setClock(C.live());
}

// Home: paused at the oldest kept moment.
function goOldest() {
    KleinLog.setFollow(false);
    setClock(C.pauseAt(rec, rec.tMin));
}

// One row up (-1) or down (+1) the Log as filtered (KleinLog.step), whichever
// tab is shown: |◀ ▶| and ↑/↓. At either end the cursor stays put. False when
// the key does nothing here.
function stepRow(dir) {
    if (!pos) return false;
    const row = KleinLog.step(pos, dir);
    if (row) goTo(row.seg, row.seq);
    return row !== false;
}

// ------------------------------------------------------------------ //
// The transport row
// ------------------------------------------------------------------ //
// |◀ ▶ ▶| and Live/Go live (End/Go to end for an opened file), from
// either tab and folded; the clock is KleinCursor.shownTime.
const prevButton = document.getElementById("tr-prev");
const nextButton = document.getElementById("tr-next");
const playButton = document.getElementById("tr-play");
const jumpButton = document.getElementById("tr-jump");
const clockEl = document.getElementById("tr-clock");
const clockParts = [".tr-main", ".tr-ms", ".tr-micro"].map((s) => clockEl.querySelector(s));
let painted = { pos: null, t: null };   // the position the transport last painted, and its time

// Where the cursor is, as the row shows it: "none" (no recording), "live",
// "end" (an opened file's end) or "past"; whether that is an opened file;
// and whether the cursor moves (live, or playing in the past).
function transportState() {
    const file = Boolean(rec && rec.source === "file");
    const state = !pos ? "none" : !pos.live ? "past" : file ? "end" : "live";
    return { state, file, playing: state === "live" || (state === "past" && pos.mode === "playing") };
}

// Every render, so only what changed is written: a new text node on every
// frame would have the browser lay the drawer out again each time.
function paintTransport() {
    const { state, file, playing } = transportState();
    prevButton.disabled = !pos;
    nextButton.disabled = !pos || pos.live;
    playButton.disabled = state === "none" || state === "end";
    paintJump(state, file);
    paintPlay(playing);
    paintClock();
}

// Live (inert), or Go live in the past; End / Go to end for an opened file.
function paintJump(state, file) {
    jumpButton.hidden = !pos;
    jumpButton.disabled = state !== "past";             // live (or at the end): a state
    if (jumpButton.dataset.state !== state) jumpButton.dataset.state = state;
    setText(jumpButton, state === "past" ? (file ? "Go to end ⏭︎" : "Go live ⏭︎")
                                         : state === "end" ? "End" : "Live");
    setAttr(jumpButton, "title", state !== "past" ? (file ? "At the file's end" : "Live")
                                 : file ? "Go to the file's end (End, Esc)" : "Back to live (End, Esc)");
}

function paintPlay(playing) {
    setText(playButton, playing ? "❚❚" : "▶︎");
    setAttr(playButton, "aria-label", playing ? "Pause" : "Play at 1×");
    setAttr(playButton, "title", playing ? "Pause (Space)"
                                         : "Play at 1× (Space) · reaching the head goes live");
}

// "14:03:22", ".201", " 030": a narrow drawer drops the µs, then the ms.
function paintClock() {
    painted = { pos, t: pos ? C.shownTime(pos, rec) : null };
    const time = pos ? formatTime(painted.t) : "";
    setText(clockParts[0], time.slice(0, -8));
    setText(clockParts[1], time.slice(-8, -4));
    setText(clockParts[2], time.slice(-4));
    clockEl.hidden = !pos;
}

// Pause at the moment shown (live: the head's seq at the time last painted,
// so nothing jumps), or play from it.
function togglePlay() {
    if (!pos) return;
    if (pos.mode === "live") {
        if (rec.source === "file") return;              // an opened file's end
        setClock(C.pause(pos.seg, pos.seq, painted.pos === pos ? painted.t : C.shownTime(pos, rec)));
    } else if (pos.mode === "playing") {
        setClock(C.pause(pos.seg, pos.seq, pos.t));
    } else {
        setClock(C.play(pos.seg, pos.seq, performance.now(), pos.t));
    }
}

prevButton.addEventListener("click", () => stepRow(-1));
nextButton.addEventListener("click", () => stepRow(1));
playButton.addEventListener("click", togglePlay);
jumpButton.addEventListener("click", goLive);
KleinLog.onPick(goTo);
paintTransport();                   // greyed until there is a recording

// ------------------------------------------------------------------ //
// The keys
// ------------------------------------------------------------------ //
// Each does what its button does (see docs/architecture.md, Keys) and says
// whether it acted: one that acts is preventDefaulted, so the drawer doesn't
// scroll; one with nothing to do here (→ or ↓ from live, End or Esc while
// live, \ on the Log) is left to the browser.
const KEYS = {
    ArrowLeft: () => stepRow(-1),
    ArrowUp: () => stepRow(-1),
    ArrowRight: () => stepRow(1),
    ArrowDown: () => stepRow(1),
    " ": (event) => !event.target.closest?.("button") && acted(togglePlay),   // a focused button keeps it
    Home: () => acted(goOldest),
    End: () => !pos.live && acted(goLive),
    Escape: () => !pos.live && acted(goLive),
    "\\": () => KleinDrawer.selectedTab() === "timeline" && acted(W.fit),
};

// fn(), a key's move that always acts.
function acted(fn) {
    fn();
    return true;
}

// Many layouts type \ with AltGr, which Windows reports as Ctrl+Alt: that
// \ counts as unmodified (a plain Ctrl+\ or Alt+\ does not).
function modified(event) {
    const altGr = event.key === "\\" && !event.metaKey && !event.shiftKey
        && (event.getModifierState("AltGraph") || (event.ctrlKey && event.altKey));
    return !altGr && (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey);
}

// Unmodified presses only, with a recording, and not in the sidebar or a text
// field.
function ignored(event) {
    return modified(event) || !pos
        || Boolean(event.target.closest?.("#sidebar, input, textarea, select"));
}

window.addEventListener("keydown", (event) => {
    const key = Object.hasOwn(KEYS, event.key) && KEYS[event.key];
    if (!key || ignored(event) || !key(event)) return;
    event.preventDefault();
});

// app.js gives its hooks (above).
function connect(h) {
    hooks = { ...hooks, ...h };
}

export { update, goLive, connect };
