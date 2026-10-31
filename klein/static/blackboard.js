// blackboard.js — the sidebar's blackboard panel: a group per board, in tree
// order, its rows' values and change marks (KleinView.rowMarks); and the keys
// linked to the cards on the canvas: a hovered key row outlines the cards
// that write and read it, a hovered card rings its keys' rows, and the cards
// writing a key that just changed pulse. app.js shows a new tree's boards
// (show), the boards at each render (render), and fills frameTrack from the
// blackboard frames; the tree's card hover and relayout reach here through
// its hooks (linkCardRows, cardsMoved). The cards are tree.js's
// (KleinTree.cards).
// See docs/architecture.md, "Rendering".

import * as KleinRenderers from "./renderers.js";
import * as KleinRecording from "./recording.js";
import * as KleinView from "./view.js";
import * as KleinTree from "./tree.js";
import { restartClass, syncKeyed, reducedMotion } from "./ui.js";

// ------------------------------------------------------------------ //
// Blackboards — one group per subtree, in tree order, values live at 2 Hz
// ------------------------------------------------------------------ //
// The list mirrors the canvas: every subtree that owns a blackboard gets a row,
// nested boards are indented under their parent, and each row is bound to the
// node it belongs to. The root board opens on load because it holds the
// mission's own state; the rest stay closed to keep the list scannable.
//
// Open/closed state lives outside the DOM so it survives every update frame.
const bbGroupOpen = {};     // board name -> is its body expanded?
const bbGroupEls = {};      // board name -> { group, header, toggle, body, count, rows, nodes }

let bbLastMode = null;          // the mode the panel was last painted in ("frames": no recording)

// Boards in tree order, from the layout — see collectBoards().
let bbBoardList = [];

const bbGroups = document.getElementById("bb-groups");
const bbCount = document.getElementById("bb-count");
const bbEmpty = document.getElementById("bb-empty");
const sidebar = document.getElementById("sidebar");

// The blackboard frames' changes over the last KleinRecording.BB_RECENT, for
// when there is no recording to show: app.js adds each frame's boards, timed
// by the gateway (`t`), and the panel marks changes from it as from a
// segment's track.
const frameTrack = KleinRecording.createTrack();

// The panel names a board by its last path segment, minus the ::uid suffix
// BehaviorTree.CPP appends to unnamed instances — "MuteRearScannerAndMoveLift",
// not "Pick_SubTree::12/MuteRearScannerAndMoveLift::17". The uid gets its own
// badge and the full path lives in the tooltip.
function boardLabel(path) {
    return path.split("/").pop().replace(/::\d+$/, "");
}

// Walk the layout depth-first for every node carrying a board. One pass yields
// tree order, nesting depth, and the node itself — which is what lets a board
// row find its card on the canvas. Board names alone could not: an instance
// named in the XML (`park_sequence`) carries no uid in its path.
function collectBoards(root) {
    const boards = [];
    (function walk(node, depth) {
        const board = node.data.board;
        if (board) {
            boards.push({
                board,
                depth,
                label: boardLabel(board),
                uid: node.data.uid,
                nodeName: node.data.name,
                isRoot: node === root,
                node,
            });
        }
        // Both branches: a collapsed subtree keeps its children in _children.
        for (const child of node.children || node._children || []) {
            walk(child, board ? depth + 1 : depth);
        }
    })(root, 0);
    return boards;
}

function createBBGroup(info) {
    const group = document.createElement("div");
    group.className = "bb-group";
    group.style.setProperty("--depth", String(info.depth));
    if (info.depth > 0) group.dataset.nested = "1";

    // A div, not a button: it carries two separate actions — open the board,
    // and find its subtree on the canvas.
    const header = document.createElement("div");
    header.className = "bb-group-header";

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "bb-group-toggle";
    toggle.title = info.board;      // the full instance path, however long
    toggle.setAttribute("aria-expanded", String(Boolean(bbGroupOpen[info.board])));

    const chevron = document.createElement("span");
    chevron.className = "bb-chevron";
    chevron.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.className = "bb-group-name";
    label.textContent = info.label;
    toggle.append(chevron, label);

    const count = document.createElement("span");
    count.className = "bb-group-count";

    header.appendChild(toggle);
    // The uid badge doubles as the locate control: it already names the card to
    // look for, so clicking it flies there.
    if (info.node) {
        const locate = document.createElement("button");
        locate.type = "button";
        locate.className = "bb-group-locate";
        locate.textContent = info.isRoot ? "root" : (info.uid == null ? "find" : `uid ${info.uid}`);
        locate.title = `Find ${info.nodeName} in the tree`;
        locate.addEventListener("click", () => KleinTree.focusNode(info.node));
        header.append(locate);
        header.addEventListener("mouseenter", () => highlightNode(info.node, true));
        header.addEventListener("mouseleave", () => highlightNode(info.node, false));
    }
    header.appendChild(count);

    const body = document.createElement("div");
    body.className = "bb-group-body";
    body.hidden = !bbGroupOpen[info.board];

    toggle.addEventListener("click", () => {
        const open = !bbGroupOpen[info.board];
        bbGroupOpen[info.board] = open;
        body.hidden = !open;
        // Stop fades on closing, or they replay on opening.
        for (const row of body.querySelectorAll(".bb-fresh")) row.classList.remove("bb-fresh");
        toggle.classList.toggle("open", open);
        toggle.setAttribute("aria-expanded", String(open));
    });
    toggle.classList.toggle("open", Boolean(bbGroupOpen[info.board]));

    group.append(header, body);
    return { board: info.board, group, header, toggle, body, count, rows: {}, nodes: [group] };
}

// Rows are buttons because they are disclosures: clicking one unwraps a value
// too long for the panel and, when the value has a renderer, reveals the
// labelled breakdown of its fields. Hovering one outlines the nodes that
// write and read the key on the canvas (linkKey).
function createBBRow(group, key) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "bb-row";

    const keyEl = document.createElement("span");
    keyEl.className = "bb-key";
    keyEl.textContent = key;
    keyEl.title = key;      // long, similar keys truncate alike; hover disambiguates
    const valueEl = document.createElement("span");
    valueEl.className = "bb-value";
    row.append(keyEl, valueEl);

    // A definition list cannot live inside a button, so the breakdown is the
    // row's sibling and the row drives its visibility.
    const detail = document.createElement("dl");
    detail.className = "bb-detail";
    detail.hidden = true;

    // tChange: the change last painted (undefined until the first paint).
    const entry = { row, value: valueEl, detail, text: null, hasDetail: false,
                    tChange: undefined, nodes: [row, detail] };
    row.addEventListener("click", () => {
        const open = !row.classList.contains("expanded");
        row.classList.toggle("expanded", open);
        detail.hidden = !(open && entry.hasDetail);
    });
    row.addEventListener("mouseenter", () => linkKey(group.board, key, true));
    row.addEventListener("mouseleave", () => linkKey(group.board, key, false));
    // The fade ran out: the change is no longer news.
    row.addEventListener("animationend", (event) => {
        if (event.animationName === "fade-out") row.classList.remove("bb-fresh");
    });
    return entry;
}

function fillBBDetail(dl, entries) {
    dl.textContent = "";
    for (const [label, text] of entries) {
        const dd = document.createElement("dd");
        dd.textContent = text;
        if (label === null) {       // a full-width block: pretty JSON, no label
            dd.className = "bb-detail-block";
            dl.appendChild(dd);
            continue;
        }
        const dt = document.createElement("dt");
        dt.textContent = label;
        dl.append(dt, dd);
    }
}

// The boards to list, board name -> its info (collectBoards'), in layout
// order first, so the panel reads like the canvas. A board the robot reports
// that the layout never mentioned is still shown, flat at the bottom — cover
// for a robot whose XML omits the instance paths.
function listBoards(boards) {
    const listed = new Map();
    for (const info of bbBoardList) {
        if (info.board in boards && !listed.has(info.board)) listed.set(info.board, info);
    }
    for (const name of Object.keys(boards)) {
        if (listed.has(name)) continue;
        listed.set(name, { board: name, depth: 0, label: name, uid: null, nodeName: name,
                           isRoot: false, node: null });
    }
    return listed;
}

// A subtree whose ports are all remapped to its parent owns nothing. It keeps
// its row so the list still mirrors the tree, but there is nothing to open.
function paintGroupHeader(group, count) {
    const isEmpty = count === 0;
    group.count.textContent = isEmpty ? "—" : String(count);
    group.group.classList.toggle("is-empty", isEmpty);
    group.toggle.disabled = isEmpty;
    group.toggle.title = isEmpty
        ? `${group.board}\nNo values of its own — its ports are remapped to the parent board.`
        : group.board;
}

function paintValue(row, value) {
    const shown = KleinRenderers.renderValue(value);
    if (row.text === shown.summary) return;
    row.value.textContent = shown.summary;
    row.value.title = KleinRenderers.exactText(value);   // exact, on hover
    row.value.classList.toggle("bb-unset", Boolean(shown.missing));
    row.text = shown.summary;
    fillBBDetail(row.detail, shown.detail);
    row.hasDetail = shown.detail.length > 0;
    row.detail.hidden = !(row.hasDetail && row.row.classList.contains("expanded"));
}

// A row's change marks at one `paint` (render): KleinView.rowMarks
// decides, this applies them and collects the keys for the writer pulse.
function markRow(row, ref, shown, paint) {
    const change = KleinRecording.bbChange(paint.track, ref.board, ref.key, paint.rule.t);
    const marks = KleinView.rowMarks({ ...paint.rule, change, shown, seen: row.tChange });
    row.tChange = change.tChange;
    const classes = row.row.classList;
    classes.toggle("bb-stream", marks.streaming);
    classes.toggle("bb-mark", marks.marked);
    if (marks.marked) paint.marked.push(linkRef(ref.board, ref.key));
    if (marks.fire) paint.fired.push(linkRef(ref.board, ref.key));
    if (marks.fade === "restart") restartClass(row.row, "bb-fresh");
    else if (marks.fade === "clear") classes.remove("bb-fresh");
}

// One board's header and rows. A closed board, or the collapsed sidebar,
// shows no row: nothing fades.
function paintGroup(group, entries, paint) {
    // The robot's map order is arbitrary (it walks an unordered_map), so
    // sort to keep rows from reshuffling under the reader between frames.
    const keys = Object.keys(entries).sort();
    paintGroupHeader(group, keys.length);
    const shown = !group.body.hidden && !sidebar.classList.contains("collapsed");
    syncKeyed(group.body, keys, group.rows, (key) => createBBRow(group, key), (row, key) => {
        paintValue(row, entries[key]);
        markRow(row, { board: group.board, key }, shown, paint);
    });
}

// `boards` as of time `t` in the blackboard `track` (a segment's, or by
// default frameTrack, the status frames' at their newest), shown in `mode`
// ("live", "playing", "paused"); `empty`: what the panel says listing no
// board. Each row's marks are KleinView.rowMarks': a mode or source switch
// only primes the rows, and the writers pulse alike. See
// docs/architecture.md, "Rendering".
function render(boards, { track = frameTrack, t = track.tLast, mode = "live",
                         empty = "This robot reports no blackboards." } = {}) {
    const painting = track === frameTrack ? "frames" : mode;
    const rule = { t, mode, primed: painting === bbLastMode, reducedMotion: reducedMotion() };
    bbLastMode = painting;
    const paint = { track, rule, marked: [], fired: [] };      // marked, fired: for the writer pulse
    const listed = listBoards(boards);
    syncKeyed(bbGroups, [...listed.keys()], bbGroupEls, (name) => createBBGroup(listed.get(name)),
              (group, name) => paintGroup(group, boards[name], paint));
    setPulses(paint.marked, paint.fired, mode === "live" || mode === "playing");

    bbCount.textContent = listed.size ? String(listed.size) : "";
    // Per-board dashes cover empty boards; this line is for having none at all.
    bbEmpty.hidden = listed.size > 0;
    bbEmpty.textContent = empty;
}

// A new tree means new boards: drop the old ones rather than leave values that
// will never update again.
function reset() {
    frameTrack.clear();
    bbGroups.textContent = "";
    for (const key of Object.keys(bbGroupEls)) delete bbGroupEls[key];
    for (const key of Object.keys(bbGroupOpen)) delete bbGroupOpen[key];
    // The root board carries the mission's own state, so it is the one worth
    // seeing without a click.
    for (const info of bbBoardList) bbGroupOpen[info.board] = info.isRoot;
    bbCount.textContent = "";
    bbEmpty.hidden = false;
    bbEmpty.textContent = "Waiting for values…";
}

// A new tree, before its cards are drawn (KleinTree.draw): its boards in tree
// order and the keys its nodes bind, and the panel emptied for them.
function show(root) {
    bbBoardList = collectBoards(root);
    bbLinks = indexBindings(root);
    linkedRef = null;
    linkedRows = [];
    linkedCard = null;
    pulseMarked = new Set();
    reset();
}

// Hovering a board row says which card it belongs to, without moving anything.
function highlightNode(node, on) {
    KleinTree.cards().filter(d => d === node)
        .select(".node-rect").classed("highlight", on);
}

// ------------------------------------------------------------------ //
// Keys and cards, linked on hover — from the layout's port bindings
// ------------------------------------------------------------------ //
// Each node lists the blackboard entries its ports reach (`bindings`, see
// docs/protocol.md): a hovered key row outlines its writers (out/inout) solid
// and its readers dashed, and accents the key on their port lines; a hovered
// card marks its keys' rows.
// Only the pulse runs per frame.
function linkRef(board, key) {
    return board + "\u0000" + key;
}
function linkOf(binding) {
    return linkRef(binding.board, binding.key);
}

// linkRef -> {writers, readers}: Sets of hierarchy nodes in tree order. A node
// that both reads and writes an entry is a writer. Built per tree (show).
let bbLinks = new Map();

function indexBindings(root) {
    const links = new Map();
    KleinRecording.eachNode(root, (node) => {
        for (const b of node.data.bindings || []) {
            const ref = linkOf(b);
            if (!links.has(ref)) links.set(ref, { writers: new Set(), readers: new Set() });
            links.get(ref)[b.dir === "in" ? "readers" : "writers"].add(node);
        }
    });
    for (const link of links.values()) for (const node of link.writers) link.readers.delete(node);
    return links;
}

// The card that stands for a node: the node's own, or the outermost folded
// ancestor's when a fold hides it.
function shownCard(node) {
    let card = node;
    for (let a = node.parent; a; a = a.parent) if (a._children) card = a;
    return card;
}

let linkedRef = null;           // the key row hovered, as a linkRef

function linkKey(board, key, on) {
    const ref = linkRef(board, key);
    if (!on && linkedRef !== ref) return;
    linkedRef = on ? ref : null;
    paintKeyLink();
}

// The hovered key's outlines and port-line accents, onto whichever cards
// stand for its nodes now (also after a fold: cardsMoved). Each card keeps
// the role last painted on it (`_linkRole`, on the element: a card that
// (re)appears is a new element, so it is painted afresh).
function paintKeyLink() {
    const link = linkedRef && bbLinks.get(linkedRef);
    const roles = new Map();
    if (link) {
        for (const node of link.readers) roles.set(shownCard(node), "read");
        for (const node of link.writers) roles.set(shownCard(node), "write");   // wins
    }
    const cards = KleinTree.cards();
    cards.each(function(d) {
        const role = roles.get(d) || null;
        if (this._linkRole === role) return;
        this._linkRole = role;
        const outline = this.querySelector(".node-link");
        outline.classList.toggle("write", role === "write");
        outline.classList.toggle("read", role === "read");
    });
    cards.selectAll("tspan.port-key")
        .classed("hl", p => linkedRef !== null && p.bindings.some(b => linkOf(b) === linkedRef));
}

let linkedRows = [];            // the rows a hovered card marked
let linkedCard = null;          // the card hovered, if any

// A hovered card's keys, marked in the panel (KleinTree's cardHovered). A
// folded card stands for the nodes it hides too, as their outlines go to it;
// clicking it to fold or unfold marks them again (cardsMoved).
function linkCardRows(card, on) {
    for (const row of linkedRows) row.classList.remove("linked");
    linkedRows = [];
    linkedCard = on ? card : null;
    if (!on) return;
    const nodes = [];
    if (card._children) KleinRecording.eachNode(card, (node) => nodes.push(node));
    else nodes.push(card);
    for (const node of nodes) {
        for (const b of node.data.bindings || []) {
            const row = bbGroupEls[b.board]?.rows[b.key]?.row;
            if (row && !row.classList.contains("linked")) {
                row.classList.add("linked");
                linkedRows.push(row);
            }
        }
    }
}

// The writer pulse: the cards writing a key that just changed, under the
// panel's own rule (KleinView.rowMarks). `pulseMarked` holds the writers of
// the keys marked statically (paused, or reduced motion); fire() starts the
// one-shot fade on the writers of a change seen live or playing.
let pulseMarked = new Set();
let pulseFiring = false;        // some card may still be fading

// The nodes writing these keys (linkRefs), each as card(node).
function writersOf(refs, card = (node) => node) {
    const nodes = new Set();
    for (const ref of refs) for (const node of bbLinks.get(ref)?.writers || []) nodes.add(card(node));
    return nodes;
}

function setPulses(markedRefs, firedRefs, moving) {
    const marked = writersOf(markedRefs);
    const same = marked.size === pulseMarked.size && [...marked].every(n => pulseMarked.has(n));
    if (!same) {
        pulseMarked = marked;
        paintPulses();
    }
    if (pulseFiring && (!moving || reducedMotion())) {
        KleinTree.cards().selectAll(".node-pulse.fire").classed("fire", false);
        pulseFiring = false;
    }
    const fired = writersOf(firedRefs, shownCard);
    if (!fired.size) return;
    pulseFiring = true;
    KleinTree.cards().filter(d => fired.has(d)).select(".node-pulse")
        .each(function() { restartClass(this, "fire"); });
}

// The static pulses, onto whichever cards stand for their writers now. Each
// card keeps the mark last painted on it (`_pulse`, on the element, as
// `_linkRole`).
function paintPulses() {
    const cards = new Set([...pulseMarked].map(shownCard));
    KleinTree.cards().each(function(d) {
        const on = cards.has(d);
        if (this._pulse === on) return;
        this._pulse = on;
        this.querySelector(".node-pulse").classList.toggle("mark", on);
    });
}

// The cards were laid out again (KleinTree: a new tree, a fold, the layout
// selector): the hovered key's outlines, the hovered card's rows and the
// static pulses go onto whichever cards stand for their nodes now.
function cardsMoved() {
    paintKeyLink();
    if (linkedCard) linkCardRows(linkedCard, true);
    paintPulses();
}

export { show, render, reset, linkCardRows, cardsMoved, frameTrack, bbGroupEls, bbGroupOpen };
