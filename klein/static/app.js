// app.js — the dashboard's entry point and composition root: index.html loads
// it as a module, and it imports the rest and connects their hooks. It holds
// the WebSocket, the one render path (KleinView.frame decides what a render
// shows; the tree canvas, the blackboard panel, the drawer, the Timeline and
// the overview paint it), the connection line and the banners, the
// sidebar's pane, and the page globals the tests and scripts use.
// See docs/architecture.md.

import * as KleinRenderers from "./renderers.js";
import * as KleinRecording from "./recording.js";
import * as KleinCursor from "./cursor.js";
import * as KleinWindow from "./window.js";
import * as KleinView from "./view.js";
import * as KleinUI from "./ui.js";
import * as KleinDrawer from "./drawer.js";
import * as KleinLog from "./log.js";
import * as KleinTransport from "./transport.js";
import * as KleinTimeline from "./timeline.js";
import * as KleinOverview from "./overview.js";
import * as KleinTree from "./tree.js";
import * as KleinBlackboard from "./blackboard.js";

// The dashboard depends on two links: browser <-> klein (this WebSocket)
// and klein <-> robot (reported by klein). We track both so the UI can
// say precisely which one is down.
let gatewayConnected = false;
let robotConnected = false;
let robotDetail = "Connecting to klein gateway…";
// Whether klein records this robot ("on", "off", "unsupported"), or shows an
// opened file with no robot ("file", klein-bt --open), from the `robot`
// frame: the drawer's chip says so, and an ended segment gives way to the
// status frames once klein says the robot is back but can't record. Null
// until klein has said.
let recordingSupport = null;

// ------------------------------------------------------------------ //
// Sidebar — resizable and collapsible like the drawer; the camera works around it
// ------------------------------------------------------------------ //
// The shown width lives in --sidebar-width on <html>, so the drawer's left
// edge and the banner stack's centre follow it in CSS alone; the camera reads
// sidebarWidth() (visibleViewport). Collapsed, a strip as wide as the
// drawer's transport row is tall keeps the fold button, as the collapsed drawer
// keeps its transport row. See docs/architecture.md, "The sidebar".
const sidebar = document.getElementById("sidebar");
const SIDEBAR_DEFAULT = 320;                // px
const SIDEBAR_MIN = 200;                    // px; at most half the window
const SIDEBAR_STRIP = 49;                   // collapsed: 48 px plus the right border

// Right is wider; dragging a collapsed sidebar opens it at the dragged width.
// Resized or folded, the drawer under the canvas changed width with it, so
// the Timeline's bars are redrawn for its new axis.
const sidebarPane = KleinUI.pane({
    el: sidebar, handle: document.getElementById("sidebar-handle"),
    button: document.getElementById("sidebar-collapse"),
    key: "klein.sidebar", sizeName: "width", cssVar: "--sidebar-width", axis: "x", grow: 1,
    initial: SIDEBAR_DEFAULT, strip: () => SIDEBAR_STRIP,
    clamp: (w) => Math.round(Math.max(SIDEBAR_MIN, Math.min(w, window.innerWidth / 2))),
    labels: ["Collapse panel", "Expand panel"], ariaLabel: true,
    onResize: () => requestRender(),
    // Folded or unfolded: nudge the view by half the change, so the tree
    // stays centred in the space that is actually visible.
    onFold: (before) => KleinTree.nudge((sidebarWidth() - before) / 2),
});

// How much of the viewport's left edge the pane covers. The canvas spans the
// whole window, so this is what keeps the tree out from under the pane.
function sidebarWidth() {
    return sidebarPane.shown();
}

// ------------------------------------------------------------------ //
// Banners — one stack of pills along the top of the canvas
// ------------------------------------------------------------------ //
// A banner is addressed by a stable `key`, so repeated news about the same
// thing replaces it instead of piling up, and any caller can take its own
// message down without knowing what else is on screen. `kind` picks the dot
// colour and the sort order (see .banner.* in styles.css); `timeout` makes it
// clear itself. A new kind of message costs one showBanner() call.
const bannerStack = document.getElementById("banner-stack");
const BANNER_FADE_MS = 450;     // must match the .banner.leaving transition
const banners = new Map();      // key -> { el, text, timers }

function showBanner(key, text, { kind = "info", timeout = 0 } = {}) {
    let entry = banners.get(key);
    if (!entry) {
        const el = bannerStack.appendChild(document.createElement("div"));
        el.dataset.key = key;
        const textEl = el.appendChild(document.createElement("span"));
        textEl.className = "banner-text";
        entry = { el, text: textEl, timers: [] };
        banners.set(key, entry);
    }
    entry.timers.forEach(clearTimeout);     // a repeat restarts the clock
    entry.timers = [];
    // Assigning the same className is a no-op, so a banner that is merely
    // re-asserted does not replay its entry animation.
    const className = `banner ${kind}`;
    if (entry.el.className !== className) entry.el.className = className;
    if (entry.text.textContent !== text) entry.text.textContent = text;
    if (timeout > 0) {
        // Removed in two steps: the element has to leave the flex stack to
        // avoid holding a gap open, and that cannot be transitioned — so it
        // fades under .leaving first, then goes.
        entry.timers.push(setTimeout(() => {
            entry.el.classList.add("leaving");
            entry.timers.push(setTimeout(() => hideBanner(key), BANNER_FADE_MS));
        }, timeout));
    }
}

function hideBanner(key) {
    const entry = banners.get(key);
    if (!entry) return;
    entry.timers.forEach(clearTimeout);
    entry.el.remove();
    banners.delete(key);
}

// Reflect both links: green when the robot is streaming, amber (plus a
// banner) when klein is up but the robot is unreachable, red when klein
// itself can't be reached.
function updateConnectionUI() {
    const dot = d3.select("#conn-dot");
    const txt = d3.select("#conn-text");

    // Freeze the RUNNING pulse whenever telemetry has stopped arriving, from
    // either link. See body.telemetry-stale in styles.css.
    document.body.classList.toggle("telemetry-stale",
                                   !gatewayConnected || !robotConnected);

    if (!gatewayConnected) {
        dot.attr("class", "dot");
        txt.text("klein gateway offline — reconnecting…");
        showBanner("connection", "Lost connection to the klein gateway — reconnecting…",
                   { kind: "error" });
    } else if (shownRecording()?.source === "file") {
        dot.attr("class", "dot file");
        txt.text(robotDetail);
        hideBanner("connection");   // no robot by design: nothing has gone wrong
    } else if (!robotConnected) {
        dot.attr("class", "dot warn");
        txt.text(robotDetail);
        showBanner("connection", robotDetail, { kind: "warn" });
    } else {
        dot.attr("class", "dot online");
        txt.text(robotDetail);
        hideBanner("connection");   // robot is streaming: nothing to report
    }
}

// ------------------------------------------------------------------ //
// One render path — what the cursor shows, painted on the next frame
// ------------------------------------------------------------------ //
// Messages only store what they carry and ask for a frame; render() decides
// what to show. While klein records, that is its recording at the cursor, and
// live is the head of the last segment. Without a recording (--record-buffer
// 0, or a robot older than BehaviorTree.CPP 4.3.3) it is the latest `layout`,
// `status` and `blackboard` frames. Either way the same painters draw it
// (KleinTree.paintStatus, KleinBlackboard.render); only the source differs.
//
// The browser's mirror of the gateway's recording (recording.js), filled from
// the WebSocket.
const recordingStore = KleinRecording.createStore();
// The one recording klein streams: the robot's or an opened file's.
function shownRecording() {
    return recordingStore.recording;
}
// The shared cursor: live, paused where the Log or the Timeline put it, or
// playing at 1x.
let clock = KleinCursor.live();
function seek(next) {
    clock = next;
    requestRender({ boards: true });
}
// Arrows, so a render or requestRender a test swaps in reaches the drawer too.
KleinDrawer.connect({ render: () => render(), requestRender: () => requestRender(), showBanner });
KleinTransport.connect({ seek });
KleinOverview.onSeek(seek);
KleinTimeline.connect({
    seek,
    toggleFold: (id) => { const d = KleinTree.nodeById(id); if (d) KleinTree.toggleFold(d); },
    foldOthers: (id) => { const d = KleinTree.nodeById(id); if (d) KleinTree.foldOthers(d); },
    glyphFor: KleinTree.glyphFor,
});
// The canvas asks for a render after a fold, scrolls the Timeline on F, keeps
// the camera right of the sidebar, and tells the blackboard panel when its
// cards move or are hovered (the panel's outlines and pulses are on them).
KleinTree.connect({
    requestRender: () => requestRender(), revealRows: KleinTimeline.revealRows, sidebarWidth,
    cardsMoved: KleinBlackboard.cardsMoved, cardHovered: KleinBlackboard.linkCardRows,
});

// The latest frames, for when there is no recording to show: the layout,
// status and blackboard frames' data, and their blackboard changes over the
// last BB_RECENT, timed by the gateway (`t`): the blackboard panel's track.
const frameTrack = KleinBlackboard.frameTrack;
const frames = { tree: null, status: {}, boards: null, track: frameTrack };

// The canvas's painter, as a binding a test can swap (Page globals).
let paintStatus = KleinTree.paintStatus;

// The root id of the tree on the canvas. Node ids are generation-prefixed (see
// tree.js, updateNodes), so an equal id is the same tree: a `segment` and a
// `layout` frame for one tree draw it once, and a same-tree restart not at all.
let drawnTreeId = null;
let displaySource = "frames";
let renderQueued = false;
let boardsDirty = false;
let boardsLive = true;          // the board panel last showed live values

function requestRender({ boards = false } = {}) {
    boardsDirty = boardsDirty || boards;
    if (renderQueued) return;
    renderQueued = true;
    // A hidden tab gets no animation frames; a (throttled) timer still keeps
    // the displayed state, and so where F goes, current.
    (document.hidden ? setTimeout : requestAnimationFrame)(() => {
        renderQueued = false;
        render();
    });
}

// A new tree: rebuild the hierarchy, then the board list and the key links
// on it, then the cards and the camera.
function showTree(treeData) {
    drawnTreeId = treeData.id;
    KleinBlackboard.show(KleinTree.show(treeData));
    KleinTree.draw();
    boardsDirty = true;
}

// Also run at once on a tab switch (drawer.js): a render already asked for
// stays queued, so playing still renders once a frame. What is shown is
// decided by KleinView.frame (view.js); this applies it.
function render() {
    const rec = shownRecording();
    KleinDrawer.showRecording(rec, recordingSupport);
    const view = KleinView.frame({ rec, clock, nowMs: performance.now(), frames,
                                   support: recordingSupport });
    clock = view.clock;
    if (view.evicted) {             // say so, rather than jump silently
        showBanner("evicted", `Older than the kept ${KleinDrawer.keptSpan(rec)}: `
                   + "moved to the oldest kept transition", { kind: "info", timeout: 6000 });
    }
    if (view.playing) requestRender({ boards: true });     // playing moves on every frame
    if (view.source !== displaySource) boardsDirty = true;
    displaySource = view.source;
    // The past: amber canvas edge and blackboard note (styles.css).
    document.body.classList.toggle("viewing-past", view.viewingPast);
    document.body.classList.toggle("no-blackboard", view.noBoards);
    KleinTransport.update(view.pos && rec, view.pos);
    KleinLog.update(view.pos && rec, view.pos);

    if (view.tree && view.tree.id !== drawnTreeId) showTree(view.tree);
    // What the Timeline's window and its views show.
    const drawerRec = view.source === "recording" ? rec : null;
    KleinWindow.sync(drawerRec, view.pos);
    KleinTimeline.update(drawerRec, view.pos, KleinTree.hierarchy());
    KleinOverview.update(drawerRec, view.pos);
    if (!KleinTree.hierarchy()) return;

    paintStatus(view.status);

    if (!boardsDirty) return;
    boardsDirty = false;
    const panel = KleinView.panel(view, frames, boardsLive);
    if (panel) KleinBlackboard.render(panel.boards, panel.opts);
    boardsLive = !view.viewingPast;
}

// Read-only facts about the mirror and the display, for the test harness
// (BrowserProbe): `displayed` is the state last painted.
function kleinDebug() {
    const rec = shownRecording();
    return { ...(rec ? KleinRecording.describe(rec) : {}),
             displaySource, displayed: KleinTree.displayed() };
}

// ------------------------------------------------------------------ //
// Gateway WebSocket connection
// ------------------------------------------------------------------ //
// Whether a recording frame can change the boards shown. A head or a gap
// can't; nor can eviction while live, which keeps each key's newest value,
// but in the past it may move the blackboard's start past the cursor.
function changesBoards(type) {
    if (type === "head" || type === "gap") return false;
    return type !== "evict" || clock.mode !== "live";
}

function connectGatewayPipeline() {
    // Same origin as this page — klein serves HTTP + WebSocket on one port.
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.binaryType = "arraybuffer";  // the recording's records frames

    ws.onopen = () => {
        gatewayConnected = true;
        updateConnectionUI();       // klein sends the robot's state right after
        // klein re-sends everything, and the first tree it names redraws the
        // canvas.
        drawnTreeId = null;
        frames.tree = null;
        frames.status = {};
        frames.boards = null;
        frameTrack.clear();
    };

    ws.onclose = () => {
        gatewayConnected = false;
        robotConnected = false;     // with klein gone we no longer know the robot
        updateConnectionUI();
        setTimeout(connectGatewayPipeline, 2000);
    };

    ws.onerror = () => ws.close();

    ws.onmessage = (event) => {
        if (typeof event.data !== "string") {   // binary: the recording's records
            recordingStore.ingest(event.data);
            requestRender();
            return;
        }
        const message = JSON.parse(event.data);
        if (recordingStore.ingest(message)) {
            requestRender({ boards: changesBoards(message.type) });
            return;
        }

        if (message.type === "layout") {
            if (!message.data) return;
            frames.tree = message.data;
            frames.status = {};      // its uids indexed the previous tree
            frames.boards = null;
            requestRender({ boards: true });
        }

        else if (message.type === "status") {
            frames.status = message.data;
            requestRender();
        }

        else if (message.type === "blackboard") {
            frames.boards = message.data || {};
            frameTrack.addBoards(message.t, frames.boards);
            frameTrack.evictBefore(message.t - KleinRecording.BB_RECENT);
            requestRender({ boards: true });
        }

        else if (message.type === "notice") {
            showBanner("notice", message.text, { kind: "info", timeout: 4000 });
        }

        else if (message.type === "robot") {
            robotConnected = message.connected;
            robotDetail = message.detail;
            recordingSupport = message.recording;
            updateConnectionUI();
            requestRender();        // the drawer's chip
        }
    };
}

window.addEventListener("DOMContentLoaded", connectGatewayPipeline);

// ------------------------------------------------------------------ //
// Page globals, for the tests and scripts
// ------------------------------------------------------------------ //
// The modules share nothing through window. The UI tests, BrowserProbe
// (tests/harness/probes.py) and scripts/ run code in the page that reads and
// drives the dashboard by name, so those names go on window here, and only
// here: the modules as app.js imports them, and app.js's own state and
// functions. Variables are accessors, so a read is current. A test can set
// recordingSupport and swap render, requestRender, showTree or paintStatus:
// app.js's own calls use the new one from then on. A few names the tests
// knew from app.js are now the canvas's (tree.js) or the blackboard panel's
// (blackboard.js), and KleinDrawer is the drawer as the tests name it:
// drawer.js, plus its Log's rows and its transport's Go live, and the clock's
// words (ui.js).
Object.assign(window, {
    KleinRenderers, KleinRecording, KleinCursor, KleinWindow, KleinView, KleinUI, KleinTree,
    KleinBlackboard, KleinLog, KleinTransport, KleinTimeline, KleinOverview,
    KleinDrawer: { ...KleinDrawer, debugRows: KleinLog.debugRows, goLive: KleinTransport.goLive,
                   formatTime: KleinUI.formatTime, formatClock: KleinUI.formatClock },
    kleinDebug, recordingStore, shownRecording, seek, frameTrack, banners, showBanner, hideBanner,
    sidebarWidth,
    svg: KleinTree.svg, zoomBehavior: KleinTree.zoomBehavior,
    visibleViewport: KleinTree.visibleViewport, runningFrontier: KleinTree.runningFrontier,
    bbGroupEls: KleinBlackboard.bbGroupEls, bbGroupOpen: KleinBlackboard.bbGroupOpen,
    renderBlackboards: KleinBlackboard.render, resetBlackboards: KleinBlackboard.reset,
});
Object.defineProperties(window, {
    clock: { get: () => clock },
    rootNodeSnapshot: { get: () => KleinTree.hierarchy() },
    renderQueued: { get: () => renderQueued },
    recordingSupport: { get: () => recordingSupport, set: (v) => { recordingSupport = v; } },
    render: { get: () => render, set: (f) => { render = f; } },
    requestRender: { get: () => requestRender, set: (f) => { requestRender = f; } },
    showTree: { get: () => showTree, set: (f) => { showTree = f; } },
    paintStatus: { get: () => paintStatus, set: (f) => { paintStatus = f; } },
});
