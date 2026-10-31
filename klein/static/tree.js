// tree.js — the tree canvas: a card per node of the tree shown (its
// geometry, labels, glyph and port line) and the links between them, folding,
// the displayed state on the cards (paintStatus), and the camera (R, F, a
// board's locate, the sidebar's nudge) aimed at the part of the window the
// canvas can be seen in. app.js shows a tree (show, then draw once the
// blackboard panel has its hierarchy), paints each render's state, and
// connects the hooks below (KleinTree.connect). The blackboard panel
// (blackboard.js) paints its key outlines and writer pulses onto the cards
// (cards) and is told when they move or are hovered.
// See docs/architecture.md, "Rendering" and "The camera".

import * as KleinRecording from "./recording.js";
import { restartClass, reducedMotion } from "./ui.js";

// app.js: ask for a render (the Timeline lists the fold), bring the
// Timeline's rows for these nodes into view (F), the sidebar's width (the
// camera's left edge), and the blackboard panel's marks on the cards: put
// them on whichever cards stand for their nodes after a relayout, and a card
// hovered or left.
let hooks = { requestRender() {}, revealRows() {}, sidebarWidth: () => 0, cardsMoved() {},
              cardHovered() {} };

// The tree on the canvas: d3's hierarchy of its layout, folds and all (a
// folded node keeps its children in _children). Null before the first tree.
let rootNodeSnapshot = null;

// ------------------------------------------------------------------ //
// Cards: geometry, labels and glyphs
// ------------------------------------------------------------------ //
const nodeWidth = 220;
// Three text rows (type, name, ports) plus even ~7px gaps above, between and
// below them. Their ink is ~28px, so the card needs ~56px for the rows to sit
// evenly; at 50 the name ends up crowding the type line above it.
const nodeHeight = 56;
const cardPadX = 12;            // left/right text inset, shared by every row

const textX = -nodeWidth / 2 + cardPadX;    // left rail every text row starts on

// The glyph sits immediately before the card's primary label, indenting that
// row only; the type caption above and the ports below keep the card's width.
const glyphGutter = 14;
const glyphX = textX + glyphGutter / 2 - 2;   // .node-glyph is anchored middle
const labelX = textX + glyphGutter;

// Text row baselines, measured from the card's centre. Spaced so the whitespace
// above the type, between type and name, between name and ports, and below the
// ports is the same ~7px — uppercase type has no descenders and the 13px name
// has a tall cap height, so even spacing needs uneven baseline steps.
const rowType = -14;            // 10px uppercase — shares its baseline with the UID
const rowName = 3;              // 13px — the status pill centres on this row
const rowPorts = 19;            // 9px monospace

// How many characters of the port summary fit on a card. .node-ports is 9px
// monospace and a monospace advance is ~0.6em, so this follows the card width
// rather than being tuned to it by hand — change nodeWidth, cardPadX or that
// font size and the limit follows instead of overflowing the card.
const maxPortChars = Math.floor((nodeWidth - cardPadX * 2) / (9 * 0.6));

// The type caption runs only as far as the UID label, less a 12px gap so the
// two never read as one word. 10px bold uppercase measures ~0.72em an advance
// including its tracking. Derived like maxPortChars so the limit follows the
// card: without it RETRYUNTILSUCCESSFUL runs straight through "UID 010".
const uidX = nodeWidth / 2 - 55;
const maxTypeChars = Math.floor((uidX - textX - 12) / (10 * 0.72));

// The primary label shares its row with the status pill, and starts after the
// glyph. 13px at ~0.51em an advance. The pill is sized for its longest label,
// "was SUCCESS" (~70px in 10px bold), plus ~5px padding a side, and keeps its
// right edge 10px in from the card's.
const pillWidth = 80;
const pillX = nodeWidth / 2 - 10 - pillWidth;
const maxLabelChars = Math.floor((pillX - labelX - 8) / (13 * 0.51));

// "vertical" is the standard BT convention: root at the top, children below,
// siblings ticked left-to-right. "horizontal" grows the tree rightward.
let orientation = "vertical";

// Ports — the attributes the tree author wrote on a node in the XML. klein
// carries them through untouched, so `if`, `num_attempts`, `case_1` and the
// rest read exactly as they do in the source tree. The card's line is every
// `port=value`, two spaces apart; `starts` maps each port to where its value
// begins in it.
function portLine(ports) {
    const starts = new Map();
    let line = "";
    for (const [port, value] of Object.entries(ports || {})) {
        line += (line ? "  " : "") + port + "=";
        starts.set(port, line.length);
        line += value;
    }
    return { starts, line };
}

// BehaviorTree.CPP writes name="Inverter" on an unnamed <Inverter>, so a card
// with no name of its own shows its type in the big row and drops the caption
// above it, rather than saying Inverter twice.
function hasOwnName(node) {
    return node.name !== node.type;
}

function primaryLabel(node) {
    return truncate(hasOwnName(node) ? node.name : node.type, maxLabelChars);
}

function typeCaption(node) {
    return hasOwnName(node) ? truncate(node.type, maxTypeChars) : "";
}

function truncate(text, limit) {
    return text.length > limit ? text.slice(0, limit - 1) + "\u2026" : text;
}

// The port line split into [{text, bindings}] whose texts join to the
// truncated line; each text naming a bound key (a binding's `at`, offsets in
// its port's value) carries the bindings it names, for the blackboard
// panel's key-row hover (blackboard.js).
function portPieces(node) {
    const { starts, line: full } = portLine(node.ports);
    const text = truncate(full, maxPortChars);
    const spans = new Map();     // "start,end" in the line -> [start, end, bindings]
    for (const b of node.bindings || []) {
        const start = starts.get(b.port);
        for (const [s, e] of b.at) {
            const name = `${start + s},${start + e}`;
            if (!spans.has(name)) spans.set(name, [start + s, start + e, []]);
            spans.get(name)[2].push(b);
        }
    }
    const tokens = [...spans.values()].sort((a, b) => a[0] - b[0]);
    if (!tokens.length) return [{ text, bindings: null }];
    const cut = text === full ? full.length : maxPortChars - 1;
    const pieces = [];
    let from = 0;
    for (const [start, end, bindings] of tokens) {
        if (start >= cut) break;
        if (start > from) pieces.push({ text: full.slice(from, start), bindings: null });
        pieces.push({ text: full.slice(start, Math.min(end, cut)), bindings });
        from = Math.min(end, cut);
    }
    if (from < text.length) pieces.push({ text: text.slice(from), bindings: null });
    return pieces;
}

// The node's category, as the robot declared it. The gateway stamps exactly one
// on every node; "Undefined" is its "the robot never said", which paints as the
// undifferentiated cyan caption.
function categoryOf(node) {
    return node.category && node.category !== "Undefined" ? node.category : null;
}

// One monochrome mark per category — the notation from Colledanchise & Ogren
// (Table 1), which Groot2 draws too, so these cards read unchanged to anyone
// who already knows that notation.
const CATEGORY_GLYPHS = {
    Control: "\u2192",       // -> ticks its children in order
    Decorator: "\u25C7",     // hollow diamond: an inner node, exactly one child
    Condition: "\u25C6",     // filled diamond: a leaf that answers a question
    Action: "\u25B8",        // filled triangle: a leaf that does work
    SubTree: "\u29C9",       // boxes within boxes
};

// Control covers Sequence, Fallback and Parallel, which the notation draws
// differently and which readers genuinely confuse. The registration name is
// already on the card, so refine whenever it is one we recognise.
const TYPE_GLYPHS = {
    Fallback: "?", ReactiveFallback: "?", AsyncFallback: "?",
    Parallel: "\u21C9", ParallelAll: "\u21C9",
};

function glyphFor(node) {
    const category = categoryOf(node);
    if (!category) return "";
    return TYPE_GLYPHS[node.type] || CATEGORY_GLYPHS[category];
}

// The card's class list: category and subtree nesting depth, the two things CSS
// paints. "node" stays first so every existing selectAll("g.node") still matches.
function nodeClasses(d) {
    const category = categoryOf(d.data);
    const depth = Math.min(d.subtreeDepth || 0, 3);
    return "node"
        + (category ? ` cat-${category}` : "")
        + (depth ? ` sub-${depth}` : "")
        // "unnamed": the primary label is the type, so it takes the type's ink.
        + (hasOwnName(d.data) ? "" : " unnamed");
}

// Everything the card had to abbreviate: type and name in full, what kind of
// node it is, the blackboard it opens, and every port one per line. Every node
// gets one — a control node has no ports, but it does have a category worth
// naming.
function nodeTitle(node) {
    const lines = [`${node.type} "${node.name}"`];
    const category = categoryOf(node);
    if (category) lines.push(`${category} node`);
    if (node.board) lines.push(`blackboard: ${node.board}`);
    const entries = Object.entries(node.ports || {});
    if (entries.length) lines.push("", ...entries.map(([key, value]) => `  ${key} = ${value}`));
    return lines.join("\n");
}

// ------------------------------------------------------------------ //
// The canvas: links, nodes and cards, and folding
// ------------------------------------------------------------------ //
// Setup scalable D3 viewport selections
const svg = d3.select("#canvas");
const gContainer = svg.append("g").attr("class", "draw-group");

// Attach infinite Pan & Zoom behaviors to the canvas
const zoomBehavior = d3.zoom()
    .scaleExtent([0.1, 3])
    .on("zoom", (event) => {
        gContainer.attr("transform", event.transform);
    });
svg.call(zoomBehavior);

// Layout spacing per orientation. d3.tree() lays siblings along x and depth
// along y; nodeSize is [sibling spacing, depth spacing] in those layout coords.
const layoutConfig = {
    vertical:   { nodeSize: [246, 130], depthStep: 130 },   // card width + subtree frame + 20
    horizontal: { nodeSize: [82, 300],  depthStep: 280 },   // card height + subtree frame + 20
};
const treeLayout = d3.tree();

// Layout coords keep x = sibling axis, y = depth axis regardless of
// orientation; the swap to screen coords happens only here at render time.
const nodeTransform = (x, y) =>
    orientation === "vertical" ? `translate(${x}, ${y})` : `translate(${y}, ${x})`;

// Lay the tree out again and move everything to its place, from (and back
// into) sourceNode's old position: the links, the nodes and, for the nodes
// that just appeared, their cards. Then the displayed state and the
// blackboard's marks go onto whichever cards stand for their nodes now.
function updateTreeLayout(sourceNode) {
    const config = layoutConfig[orientation];
    const treeData = treeLayout.nodeSize(config.nodeSize)(rootNodeSnapshot);
    const nodesList = treeData.descendants();
    const linksList = treeData.links();

    // Normalize depth spacing
    nodesList.forEach(d => d.y = d.depth * config.depthStep);

    updateLinks(linksList, sourceNode);
    updateNodes(nodesList, sourceNode);

    // Cache positions for the next transition's origin
    nodesList.forEach(d => {
        d.x0 = d.x;
        d.y0 = d.y;
    });

    // Cards that just (re)appeared, e.g. by unfolding, show the displayed state
    // now rather than IDLE until the next frame; and a fold moves an outline
    // or a pulse to the card that now stands for its node.
    paintCards(lastStatusMap);
    hooks.cardsMoved();
}

// The edges (cubic Bézier curves), keyed on stable node id: entering from
// sourceNode's old position, leaving into its new one.
function updateLinks(linksList, sourceNode) {
    const linkSelection = gContainer.selectAll("path.link")
        .data(linksList, d => d.target.data.id);

    const linkEnter = linkSelection.enter().append("path")
        .attr("class", "link")
        .attr("d", () => {
            const o = { x: sourceNode.x0 || 0, y: sourceNode.y0 || 0 };
            return diagonalCurve({ source: o, target: o });
        });

    linkEnter.merge(linkSelection).transition().duration(250)
        .attr("d", diagonalCurve);

    linkSelection.exit().transition().duration(250)
        .attr("d", () => {
            const o = { x: sourceNode.x, y: sourceNode.y };
            return diagonalCurve({ source: o, target: o });
        })
        .remove();
}

// The node groups, keyed on stable node id (uid may be null): a card for each
// one entering (appendCards), then all moved to their places.
function updateNodes(nodesList, sourceNode) {
    const nodeSelection = gContainer.selectAll("g.node")
        .data(nodesList, d => d.data.id);

    const nodeEnter = nodeSelection.enter().append("g")
        // (Re)appeared: force the status repaint (paintCards).
        .each(d => { d._statusKey = null; })
        .attr("transform", nodeTransform(sourceNode.x0 || 0, sourceNode.y0 || 0))
        .on("click", (event, d) => {
            if (event.defaultPrevented) return;
            toggleFold(d);
        })
        .on("mouseenter", (event, d) => hooks.cardHovered(d, true))
        .on("mouseleave", (event, d) => hooks.cardHovered(d, false));
    appendCards(nodeEnter);

    // Category and subtree depth, set on the merge rather than on enter: a
    // reconnect re-sends the cached layout with identical ids, so d3 matches the
    // existing cards and nothing enters — classing on enter would silently skip
    // repainting them. Safe to write the whole list, because nothing else puts a
    // class on g.node (running/focused/highlight all live on its children).
    //
    // A card's *labels* stay on enter (appendCards), and are safe there because
    // the gateway generation-prefixes node ids: two trees' ids are disjoint, so
    // a layout for a different tree retires every card rather than matching it.
    // (The subtree frame relies on the same guarantee.) A reconnect does match,
    // but it replays the identical cached layout, so there is nothing to
    // rewrite — and this function also runs on every expand/collapse, where
    // re-labelling every visible card would be pure waste.
    const nodeUpdate = nodeEnter.merge(nodeSelection);
    nodeUpdate.attr("class", nodeClasses);

    nodeUpdate.transition().duration(250)
        .attr("transform", d => nodeTransform(d.x, d.y));

    nodeSelection.exit().transition().duration(250)
        .attr("transform", nodeTransform(sourceNode.x, sourceNode.y))
        .remove();
}

// The parts of each card entering, in paint order.
function appendCards(nodeEnter) {
    // Native tooltip. First child, as SVG wants <title> to be, and on every
    // node: the cards with no ports are the control and decorator nodes whose
    // category the tooltip spells out.
    nodeEnter.append("title")
        .text(d => nodeTitle(d.data));

    // Membership of a subtree: a hairline ring 3px outside the card, on every
    // card in the region rather than only the one that opens it. Kept a separate
    // element from the card's own stroke, because that stroke is the status
    // channel — this way a node inside a subtree still shows whether it
    // succeeded or failed. Appended before the card so it paints behind it and
    // can never overdraw that stroke.
    nodeEnter.filter(d => (d.subtreeDepth || 0) > 0)
        .append("rect")
        .attr("class", "node-subtree-frame")
        .attr("x", -nodeWidth / 2 - 3)
        .attr("y", -nodeHeight / 2 - 3)
        .attr("width", nodeWidth + 6)
        .attr("height", nodeHeight + 6)
        .attr("rx", 9)
        .attr("ry", 9);

    // A hovered key row's writers (solid) and readers (dashed): an accent
    // outline just outside the card and its subtree ring, so the status
    // stroke stays readable underneath. Hidden until a row is hovered.
    nodeEnter.append("rect")
        .attr("class", "node-link")
        .attr("x", -nodeWidth / 2 - 6)
        .attr("y", -nodeHeight / 2 - 6)
        .attr("width", nodeWidth + 12)
        .attr("height", nodeHeight + 12)
        .attr("rx", 11)
        .attr("ry", 11);

    // Node card background, centered on the node's layout point so the card
    // needs no per-orientation adjustments
    nodeEnter.append("rect")
        .attr("class", "node-rect")
        .attr("width", nodeWidth)
        .attr("height", nodeHeight)
        .attr("rx", 6)
        .attr("ry", 6)
        .attr("x", -nodeWidth / 2)
        .attr("y", -nodeHeight / 2)
        .style("stroke", "var(--color-IDLE)");

    // Type tag — the qualifier above the name, when there is a name to qualify.
    nodeEnter.append("text")
        .attr("class", "node-type")
        .attr("x", textX)
        .attr("y", rowType)
        .text(d => typeCaption(d.data));

    // What kind of node this is, as a mark rather than a word — the cue that
    // stays legible once the words beside it are too small to read.
    nodeEnter.append("text")
        .attr("class", "node-glyph")
        .attr("x", glyphX)
        .attr("y", rowName)
        .text(d => glyphFor(d.data));

    // The card's primary label, beside the glyph.
    nodeEnter.append("text")
        .attr("class", "node-name")
        .attr("x", labelX)
        .attr("y", rowName)
        .text(d => primaryLabel(d.data));

    // Ports the tree author wrote on this node, along the card's bottom edge —
    // what a Precondition actually tests, which case a Switch matched. Blank
    // for a node with no ports; the untruncated set is in the hover title.
    // A key's name is its own tspan (portPieces), for the key-row hover.
    nodeEnter.append("text")
        .attr("class", "node-ports")
        .attr("x", textX)
        .attr("y", rowPorts)
        .selectAll("tspan")
        .data(d => portPieces(d.data))
        .join("tspan")
        .attr("class", p => p.bindings ? "port-key" : null)
        .text(p => p.text);

    // UID label (blank when this node carries no UID)
    nodeEnter.append("text")
        .attr("class", "node-uid")
        .attr("x", uidX)
        .attr("y", rowType)
        .text(d => d.data.uid == null ? "" : `UID ${String(d.data.uid).padStart(3, '0')}`);

    // Status pill background
    nodeEnter.append("rect")
        .attr("class", "status-pill")
        .attr("x", pillX)
        .attr("y", rowName - 12)
        .attr("width", pillWidth)
        .attr("height", 16)
        .attr("rx", 3)
        .style("fill", "var(--color-IDLE)");

    // Status text
    nodeEnter.append("text")
        .attr("class", "node-status-text")
        .attr("x", pillX + pillWidth / 2)
        .attr("y", rowName)
        .attr("text-anchor", "middle")
        .text("IDLE");

    // The writer pulse: a dot at the card's corner when a key it writes
    // changes (blackboard.js, paintPulses).
    nodeEnter.append("circle")
        .attr("class", "node-pulse")
        .attr("cx", nodeWidth / 2 - 4)
        .attr("cy", -nodeHeight / 2 + 4)
        .attr("r", 3.5)
        .on("animationend", function() { this.classList.remove("fire"); });
}

// Fold or unfold one node: its card's click, and its Timeline row's chevron.
// The Timeline lists the canvas's own fold, so it follows on the next render.
function toggleFold(d) {
    if (d.children) {
        d._children = d.children;
        d.children = null;
    } else {
        d.children = d._children;
        d._children = null;
    }
    updateTreeLayout(d);
    hooks.requestRender();
}

// Alt-click on a Timeline chevron: fold every subtree except this node's own
// (it and its ancestors are unfolded), so only its section stays open.
function foldOthers(d) {
    const keep = new Set(d.ancestors());
    KleinRecording.eachNode(rootNodeSnapshot, (node) => {
        if (keep.has(node)) {
            if (node._children) {
                node.children = node._children;
                node._children = null;
            }
        } else if (node.data.is_subtree_root && node.children) {
            node._children = node.children;
            node.children = null;
        }
    });
    updateTreeLayout(rootNodeSnapshot);
    hooks.requestRender();
}

// The hierarchy node with this layout id, folded away or not.
function nodeById(id) {
    let found = null;
    KleinRecording.eachNode(rootNodeSnapshot, (node) => {
        if (node.data.id === id) found = node;
    });
    return found;
}

// Cubic Bézier connector drawn card-edge to card-edge: parent bottom-center
// to child top-center when vertical, parent right to child left when
// horizontal. Coords are layout coords (x = sibling axis, y = depth axis).
function diagonalCurve({ source, target }) {
    if (orientation === "vertical") {
        const sY = source.y + nodeHeight / 2;
        const tY = target.y - nodeHeight / 2;
        return `M ${source.x} ${sY}
                C ${source.x} ${(sY + tY) / 2},
                  ${target.x} ${(sY + tY) / 2},
                  ${target.x} ${tY}`;
    }
    const sY = source.y + nodeWidth / 2;
    const tY = target.y - nodeWidth / 2;
    return `M ${sY} ${source.x}
            C ${(sY + tY) / 2} ${source.x},
              ${(sY + tY) / 2} ${target.x},
              ${tY} ${target.x}`;
}

// ------------------------------------------------------------------ //
// A new tree
// ------------------------------------------------------------------ //
// A new tree: its hierarchy, with no state shown on it yet. Returned for the
// blackboard panel (KleinBlackboard.show); draw() then puts it on the canvas.
function show(treeData) {
    rootNodeSnapshot = d3.hierarchy(treeData);
    rootNodeSnapshot.x0 = 0;
    rootNodeSnapshot.y0 = 0;
    tagSubtreeDepth(rootNodeSnapshot);
    lastStatusMap = {};      // its uids indexed the tree we just dropped
    lastActiveStatusMap = {};
    return rootNodeSnapshot;
}

// Lay the whole tree out afresh and fit the camera to it: a new tree, or the
// layout selector.
function draw() {
    updateTreeLayout(rootNodeSnapshot);
    resetCamera();
}

// The hierarchy on the canvas, or null before the first tree: what the
// Timeline lists, with the canvas's fold.
function hierarchy() {
    return rootNodeSnapshot;
}

// The cards on the canvas: a d3 selection of g.node, each bound to its
// hierarchy node.
function cards() {
    return gContainer.selectAll("g.node");
}

// How deep in *subtree nesting* each node sits, which is what the card fill
// encodes. The root's own board is the mission, not a subtree, so it stays
// level 0; a <SubTree> card and everything under it is level 1, one nested
// inside that is level 2. A SubTree node takes the level it *opens* rather than
// its parent's, so the region reads as one slab with its own name at the top.
//
// _children included, so a collapsed subtree keeps its level and comes back at
// the right tint when it is reopened.
function tagSubtreeDepth(root) {
    KleinRecording.eachNode(root, (node) => {
        node.subtreeDepth = (node.parent ? node.parent.subtreeDepth : 0)
                            + (node.data.is_subtree_root ? 1 : 0);
    });
}

// ------------------------------------------------------------------ //
// Live status coloring — updates strokes/pills in place, no re-render
// ------------------------------------------------------------------ //
// The displayed state (app.js's render()), kept whole. The per-node
// `_statusKey` cache below is a *DOM* cache — a collapsed subtree has no
// cards, so it holds nothing for exactly the nodes runningFrontier() most
// needs. This map is by uid and so covers the whole tree, folded away or not.
let lastStatusMap = {};
// The last displayed state in which anything was RUNNING. Once the tree
// finishes, this is where the action last was, and so where F goes.
let lastActiveStatusMap = {};

// Show statusMap, {uid: {status, from}}, on the cards: render()'s displayed
// state, kept for the cards a fold brings back and for F.
function paintStatus(statusMap) {
    lastStatusMap = statusMap;
    if (Object.values(lastStatusMap).some(entry => entry.status === "RUNNING")) {
        lastActiveStatusMap = lastStatusMap;
    }
    paintCards(lastStatusMap);
}

// The state last shown (paintStatus), for the test harness (kleinDebug).
function displayed() {
    return lastStatusMap;
}

// A state onto the cards there are, skipping the cards whose status is
// unchanged.
function paintCards(telemetryMap) {
    gContainer.selectAll("g.node").each(function(d) {
        if (d.data.uid == null) return;             // node has no UID to match
        const entry = telemetryMap[d.data.uid];     // { status, from }
        if (!entry) return;

        // Skip the DOM writes entirely when this node's status is unchanged.
        const key = entry.from ? `IDLE:${entry.from}` : entry.status;
        if (d._statusKey === key) return;
        d._statusKey = key;

        const isRunning = entry.status === "RUNNING";
        const isTransition = entry.from != null;    // "just became IDLE, was X"
        const statusColor = `var(--color-${entry.status})`;
        const strokeColor = isTransition ? "var(--color-TRANSITION)" : statusColor;
        const label = isTransition ? "was " + entry.from : entry.status;

        const el = d3.select(this);
        el.select(".node-rect")
            .style("stroke", strokeColor)
            .classed("running", isRunning);
        el.select(".status-pill")
            .style("fill", statusColor)
            .classed("running", isRunning);
        el.select(".node-status-text").text(label);
    });
}

// ------------------------------------------------------------------ //
// The camera
// ------------------------------------------------------------------ //
// Move the view dx px across without throwing away the reader's zoom/pan:
// the sidebar was folded or unfolded, and half the change keeps the tree
// centred in the space that is actually visible.
function nudge(dx) {
    if (!rootNodeSnapshot) return;
    const current = d3.zoomTransform(svg.node());
    const shifted = d3.zoomIdentity
        .translate(current.x + dx, current.y)
        .scale(current.k);
    svg.transition().duration(220).call(zoomBehavior.transform, shifted);
}

// The part of the window the canvas can actually be seen in: right of the
// sidebar, below the banner strip and above the drawer. The svg spans the
// whole window under them, so every camera move aims at this instead. Its bottom is the top of the hint
// line (#watermark), which rides just above the drawer: a fitted tree would
// otherwise put its last row under it.
const watermark = document.getElementById("watermark");

// The top strip is always kept clear for one banner pill (#banner-stack:
// 12px from the top, a 34px pill, plus a margin), so the camera never puts
// the tree under a pill (a connection warning, a saved file) and a pill
// coming or going doesn't move it. A second pill at once may overlap the
// tree's top for a moment.
const BANNER_INSET = 12 + 34 + 10;

function visibleViewport() {
    const left = hooks.sidebarWidth();
    return { left, top: BANNER_INSET, width: window.innerWidth - left,
             height: watermark.getBoundingClientRect().top - BANNER_INSET };
}

// Breathing room around a framed group, in layout units — roughly a third of a
// card, enough that the framed cards never sit against the window edge.
const framePad = 60;

// The screen-oriented box (x across, y down, layout units) around these cards.
// Cards are centre-anchored, and layout coords are (sibling, depth) — the swap
// to screen coords is the same one nodeTransform() makes.
function cardBounds(nodes) {
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const node of nodes) {
        const [x, y] = orientation === "vertical" ? [node.x, node.y] : [node.y, node.x];
        minX = Math.min(minX, x - nodeWidth / 2);
        maxX = Math.max(maxX, x + nodeWidth / 2);
        minY = Math.min(minY, y - nodeHeight / 2);
        maxY = Math.max(maxY, y + nodeHeight / 2);
    }
    return { minX, maxX, minY, maxY };
}

// How long R, F and a blackboard focus fly the camera: no flight at all for a
// reader who asked the system for reduced motion.
function cameraMs() {
    return reducedMotion() ? 0 : 500;
}

// The scale at which a box (plus framePad all round) just fits the view.
function fitScale(box, view) {
    return Math.min(view.width / (box.maxX - box.minX + framePad * 2),
                    view.height / (box.maxY - box.minY + framePad * 2));
}

// Fit the whole (unfolded) tree into the visible viewport, with the root at
// the conventional entry point: the tree's top edge at the top, centred
// across, for vertical trees; its left edge at the left, centred down, for
// horizontal ones. Never zooms in past 1:1, so a small tree isn't blown up.
function resetCamera() {
    const view = visibleViewport();
    const box = cardBounds(rootNodeSnapshot.descendants());
    // Clamped here rather than left to d3: it silently clamps the transform it
    // stores, which would leave the translate below computed for another scale.
    const [minScale] = zoomBehavior.scaleExtent();
    const scale = Math.max(minScale, Math.min(1, fitScale(box, view)));
    const target = orientation === "vertical"
        ? d3.zoomIdentity.translate(view.left + view.width / 2 - (box.minX + box.maxX) / 2 * scale,
                                    view.top + (framePad - box.minY) * scale)
        : d3.zoomIdentity.translate(view.left + (framePad - box.minX) * scale,
                                    view.top + view.height / 2 - (box.minY + box.maxY) / 2 * scale);
    svg.transition().duration(cameraMs()).call(zoomBehavior.transform, target.scale(scale));
}

// Open every collapsed ancestor of these nodes, so each one has a card again.
// Says whether anything moved: the caller has to relayout before reading x/y,
// and a node the reader never folded away costs nothing.
function revealAncestors(nodes) {
    let reopened = false;
    for (const node of nodes) {
        for (let ancestor = node.parent; ancestor; ancestor = ancestor.parent) {
            if (ancestor._children) {
                ancestor.children = ancestor._children;
                ancestor._children = null;
                reopened = true;
            }
        }
    }
    return reopened;
}

// Fly the camera to one node and pulse its card — the blackboard panel's answer
// to "where is this subtree?". Collapsed ancestors are reopened first, since a
// board can belong to a subtree the reader has folded away.
function focusNode(node) {
    if (!node || !rootNodeSnapshot) return;
    if (revealAncestors([node])) updateTreeLayout(rootNodeSnapshot);   // sets node.x/y

    const scale = 0.9;
    const [x, y] = orientation === "vertical" ? [node.x, node.y] : [node.y, node.x];
    const view = visibleViewport();
    svg.transition().duration(cameraMs()).call(
        zoomBehavior.transform,
        d3.zoomIdentity.translate(view.left + view.width / 2 - x * scale,
                                  view.top + view.height / 2 - y * scale).scale(scale)
    );
    pulseNodes([node]);
}

// The tick's leading edge: every RUNNING node with no RUNNING node beneath it.
// A Sequence is RUNNING for as long as the child it is waiting on is, so the
// whole spine from the root down reads RUNNING — the leaves are the only part
// that answers "what is the robot doing?". A Parallel puts several there at once.
//
// Walked over the hierarchy, not the canvas: a collapsed subtree keeps its
// children in _children and has no cards at all, and that is precisely where a
// reader who folded the tree down has lost track of the action.
function runningFrontier(statusMap = lastStatusMap) {
    if (!rootNodeSnapshot) return [];
    const frontier = [];
    (function walk(node) {
        const entry = statusMap[node.data.uid];
        const before = frontier.length;
        for (const child of node.children || node._children || []) walk(child);
        // Nothing below it is running, so this node is where the tick stops.
        if (entry && entry.status === "RUNNING" && frontier.length === before) {
            frontier.push(node);
        }
    })(rootNodeSnapshot);
    return frontier;
}

// Fit a set of cards in the visible canvas. Unlike focusNode this keeps the
// reader's zoom whenever the group already fits, and only ever zooms *out* —
// a single running leaf should not throw the reader to 3x, and holding the
// scale makes a second press a no-op rather than a lurch.
function frameNodes(nodes) {
    if (!nodes.length) return;
    const view = visibleViewport();
    const box = cardBounds(nodes);
    const [minScale] = zoomBehavior.scaleExtent();      // see resetCamera
    const scale = Math.max(minScale, Math.min(d3.zoomTransform(svg.node()).k, fitScale(box, view)));

    const centerX = (box.minX + box.maxX) / 2;
    const centerY = (box.minY + box.maxY) / 2;
    svg.transition().duration(cameraMs()).call(
        zoomBehavior.transform,
        d3.zoomIdentity
            .translate(view.left + view.width / 2 - centerX * scale,
                       view.top + view.height / 2 - centerY * scale)
            .scale(scale)
    );
}

// Take the camera to whatever the robot is doing right now. A finished tree has
// no frontier, so go to where it last had one; a tree that has never run has
// nothing truer to show than the whole tree.
function focusAction() {
    if (!rootNodeSnapshot) return;
    let frontier = runningFrontier();
    if (!frontier.length) frontier = runningFrontier(lastActiveStatusMap);
    if (!frontier.length) {
        resetCamera();
        return;
    }
    if (revealAncestors(frontier)) updateTreeLayout(rootNodeSnapshot);   // sets x/y
    frameNodes(frontier);
    pulseNodes(frontier);
    hooks.revealRows(frontier.map(node => node.data.id));
}

let pulseTimer = null;

function pulseNodes(nodes) {
    const wanted = new Set(nodes);
    gContainer.selectAll(".node-rect.focused").classed("focused", false);
    const rects = gContainer.selectAll("g.node").filter(d => wanted.has(d)).select(".node-rect");
    rects.each(function() { restartClass(this, "focused"); });
    clearTimeout(pulseTimer);
    pulseTimer = setTimeout(
        () => gContainer.selectAll(".node-rect.focused").classed("focused", false), 1100);
}

// Camera keys. Two, because there are only two questions the camera is ever
// asked: where is the action, and where is everything.
//
// Unmodified presses only — Ctrl/Cmd+R still reloads the page and Cmd+F still
// opens the browser's find bar. The sidebar and the drawer are full of real
// buttons, radios and disclosures, so keys pressed inside them belong to
// whatever has focus there.
window.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (!rootNodeSnapshot) return;
    if (event.target.closest?.("#sidebar, #drawer")) return;

    const key = event.key.toLowerCase();
    if (key === "f") focusAction();
    else if (key === "r") resetCamera();
    else return;
    event.preventDefault();
});

// Layout selector — reflow the same hierarchy and re-aim the camera; the
// 250ms node/link transitions animate the change.
for (const input of document.querySelectorAll('input[name="layout"]')) {
    input.addEventListener("change", () => {
        if (!input.checked) return;
        orientation = input.value;
        if (rootNodeSnapshot) draw();
    });
}

// The legend's marks come from the tables above, so the swatches and the canvas
// cannot drift; its colours come from the same .cat-* rules the cards use.
function fillLegendGlyphs() {
    for (const el of document.querySelectorAll("#legend [data-cat]")) {
        el.textContent = CATEGORY_GLYPHS[el.dataset.cat] || "";
    }
    for (const el of document.querySelectorAll("#legend [data-type]")) {
        el.textContent = TYPE_GLYPHS[el.dataset.type] || "";
    }
}

window.addEventListener("DOMContentLoaded", fillLegendGlyphs);

// app.js gives its hooks (above).
function connect(h) {
    hooks = { ...hooks, ...h };
}

export {
    show, draw, hierarchy, paintStatus, displayed, nodeById, toggleFold, foldOthers, glyphFor,
    cards, focusNode, nudge, visibleViewport, runningFrontier, svg, zoomBehavior, connect,
};
