let rootNodeSnapshot = null;

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
// rest read exactly as they do in the source tree.
function portSummary(ports) {
    if (!ports) return "";
    return Object.entries(ports).map(([key, value]) => `${key}=${value}`).join("  ");
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

function updateTreeLayout(sourceNode) {
    const config = layoutConfig[orientation];
    const treeData = treeLayout.nodeSize(config.nodeSize)(rootNodeSnapshot);
    const nodesList = treeData.descendants();
    const linksList = treeData.links();

    // Normalize depth spacing
    nodesList.forEach(d => d.y = d.depth * config.depthStep);

    // 1. RENDER EDGES / LINKS (cubic Bézier curves), keyed on stable node id
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

    // 2. RENDER NODE GROUPS, keyed on stable node id (uid may be null)
    const nodeSelection = gContainer.selectAll("g.node")
        .data(nodesList, d => d.data.id);

    const nodeEnter = nodeSelection.enter().append("g")
        .each(d => { d._statusKey = null; })   // (re)appeared: force the repaint below
        .attr("transform", nodeTransform(sourceNode.x0 || 0, sourceNode.y0 || 0))
        .on("click", (event, d) => {
            if (event.defaultPrevented) return;
            toggleFold(d);
        });

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
    nodeEnter.append("text")
        .attr("class", "node-ports")
        .attr("x", textX)
        .attr("y", rowPorts)
        .text(d => truncate(portSummary(d.data.ports), maxPortChars));

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

    // Merge + animate to final positions
    const nodeUpdate = nodeEnter.merge(nodeSelection);

    // Category and subtree depth, set on the merge rather than on enter: a
    // reconnect re-sends the cached layout with identical ids, so d3 matches the
    // existing cards and nothing enters — classing on enter would silently skip
    // repainting them. Safe to write the whole list, because nothing else puts a
    // class on g.node (running/focused/highlight all live on its children).
    //
    // A card's *labels* stay on enter, and are safe there because the gateway
    // generation-prefixes node ids: two trees' ids are disjoint, so a layout for
    // a different tree retires every card rather than matching it. (The subtree
    // frame above relies on the same guarantee.) A reconnect does match, but it
    // replays the identical cached layout, so there is nothing to rewrite — and
    // this function also runs on every expand/collapse, where re-labelling every
    // visible card would be pure waste.
    nodeUpdate.attr("class", nodeClasses);

    nodeUpdate.transition().duration(250)
        .attr("transform", d => nodeTransform(d.x, d.y));

    nodeSelection.exit().transition().duration(250)
        .attr("transform", nodeTransform(sourceNode.x, sourceNode.y))
        .remove();

    // Cache positions for the next transition's origin
    nodesList.forEach(d => {
        d.x0 = d.x;
        d.y0 = d.y;
    });

    // Cards that just (re)appeared, e.g. by unfolding, show the displayed state
    // now rather than IDLE until the next frame.
    paintStatus(lastStatusMap);
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
    requestRender();
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
    requestRender();
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
// Live status coloring — updates strokes/pills in place, no re-render
// ------------------------------------------------------------------ //
// The displayed state (see render()), kept whole. The per-node `_statusKey`
// cache below is a *DOM* cache — a collapsed subtree has no cards, so it holds
// nothing for exactly the nodes runningFrontier() most needs. This map is by uid
// and so covers the whole tree, folded away or not.
let lastStatusMap = {};
// The last displayed state in which anything was RUNNING. Once the tree
// finishes, this is where the action last was, and so where F goes.
let lastActiveStatusMap = {};

function paintStatus(telemetryMap) {
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
// Blackboards — one group per subtree, in tree order, values live at 2 Hz
// ------------------------------------------------------------------ //
// The list mirrors the canvas: every subtree that owns a blackboard gets a row,
// nested boards are indented under their parent, and each row is bound to the
// node it belongs to. The root board opens on load because it holds the
// mission's own state; the rest stay closed to keep the list scannable.
//
// Open/closed state and the last-seen values live outside the DOM so they
// survive every update frame.
const bbGroupOpen = {};     // board name -> is its body expanded?
const bbLastValues = {};    // "board key" -> last value, serialized, for the flash
const bbGroupEls = {};      // board name -> { group, header, toggle, body, count, rows }

// Boards in tree order, from the layout — see collectBoards().
let bbBoardList = [];

const bbGroups = document.getElementById("bb-groups");
const bbCount = document.getElementById("bb-count");
const bbEmpty = document.getElementById("bb-empty");

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
        locate.addEventListener("click", () => focusNode(info.node));
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
        toggle.classList.toggle("open", open);
        toggle.setAttribute("aria-expanded", String(open));
    });
    toggle.classList.toggle("open", Boolean(bbGroupOpen[info.board]));

    group.append(header, body);
    bbGroups.appendChild(group);
    return { group, header, toggle, body, count, rows: {} };
}

// Rows are buttons because they are disclosures: clicking one unwraps a value
// too long for the panel and, when the value has a renderer, reveals the
// labelled breakdown of its fields.
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

    const entry = { row, value: valueEl, detail, text: null, hasDetail: false };
    row.addEventListener("click", () => {
        const open = !row.classList.contains("expanded");
        row.classList.toggle("expanded", open);
        detail.hidden = !(open && entry.hasDetail);
    });

    group.body.append(row, detail);
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

// Reorder children only when the order is actually wrong: re-appending a row
// restarts its flash animation.
function syncBBOrder(container, ordered) {
    const correct = ordered.length === container.children.length
        && ordered.every((el, i) => container.children[i] === el);
    if (!correct) ordered.forEach(el => container.appendChild(el));
}

// `flash: false` paints without flashing changed rows: for a cursor moving
// through the past, where every step changes something.
//
// `empty` is what the panel says when there are no boards at all.
function renderBlackboards(boards, { flash = true, empty = "This robot reports no blackboards." } = {}) {
    // Layout order first, so the panel reads like the canvas. A board the robot
    // reports that the layout never mentioned is still shown, flat at the
    // bottom — cover for a robot whose XML omits the instance paths.
    const known = new Set(bbBoardList.map(info => info.board));
    const listed = [
        ...bbBoardList.filter(info => info.board in boards),
        ...Object.keys(boards).filter(name => !known.has(name)).map(name => ({
            board: name, depth: 0, label: name, uid: null, nodeName: name,
            isRoot: false, node: null,
        })),
    ];

    for (const info of listed) {
        const name = info.board;
        const group = bbGroupEls[name] || (bbGroupEls[name] = createBBGroup(info));
        const entries = boards[name];
        // The robot's map order is arbitrary (it walks an unordered_map), so
        // sort to keep rows from reshuffling under the reader between frames.
        const keys = Object.keys(entries).sort();

        // A subtree whose ports are all remapped to its parent owns nothing. It
        // keeps its row so the list still mirrors the tree, but there is
        // nothing to open.
        const isEmpty = keys.length === 0;
        group.count.textContent = isEmpty ? "—" : String(keys.length);
        group.group.classList.toggle("is-empty", isEmpty);
        group.toggle.disabled = isEmpty;
        group.toggle.title = isEmpty
            ? `${name}\nNo values of its own — its ports are remapped to the parent board.`
            : name;

        for (const key of keys) {
            const row = group.rows[key] || (group.rows[key] = createBBRow(group, key));
            const value = entries[key];
            const render = KleinRenderers.renderValue(value);
            if (row.text !== render.summary) {
                row.value.textContent = render.summary;
                row.value.title = KleinRenderers.exactText(value);   // exact, on hover
                row.value.classList.toggle("bb-unset", Boolean(render.missing));
                row.text = render.summary;
                fillBBDetail(row.detail, render.detail);
                row.hasDetail = render.detail.length > 0;
                row.detail.hidden = !(row.hasDetail && row.row.classList.contains("expanded"));
            }

            // Flash on a real change only — not the first time a key is seen.
            const stateKey = name + " " + key;
            const serialized = JSON.stringify(value === undefined ? null : value);
            if (flash && stateKey in bbLastValues && bbLastValues[stateKey] !== serialized) {
                row.row.classList.remove("bb-changed");
                void row.row.offsetWidth;        // restart a flash already in flight
                row.row.classList.add("bb-changed");
            } else if (!flash) {
                row.row.classList.remove("bb-changed");     // a live flash still running
            }
            bbLastValues[stateKey] = serialized;
        }

        for (const key of Object.keys(group.rows)) {     // keys the robot dropped
            if (!(key in entries)) {
                group.rows[key].row.remove();
                group.rows[key].detail.remove();
                delete group.rows[key];
                delete bbLastValues[name + " " + key];
            }
        }
        syncBBOrder(group.body,
            keys.flatMap(key => [group.rows[key].row, group.rows[key].detail]));
    }

    const names = listed.map(info => info.board);
    for (const name of Object.keys(bbGroupEls)) {        // boards the robot dropped
        if (!names.includes(name)) {
            bbGroupEls[name].group.remove();
            delete bbGroupEls[name];
        }
    }
    syncBBOrder(bbGroups, names.map(name => bbGroupEls[name].group));

    bbCount.textContent = names.length ? String(names.length) : "";
    // Per-board dashes cover empty boards; this line is for having none at all.
    bbEmpty.hidden = names.length > 0;
    bbEmpty.textContent = empty;
}

// A new tree means new boards: drop the old ones rather than leave values that
// will never update again.
function resetBlackboards() {
    bbGroups.textContent = "";
    for (const key of Object.keys(bbGroupEls)) delete bbGroupEls[key];
    for (const key of Object.keys(bbLastValues)) delete bbLastValues[key];
    for (const key of Object.keys(bbGroupOpen)) delete bbGroupOpen[key];
    // The root board carries the mission's own state, so it is the one worth
    // seeing without a click.
    for (const info of bbBoardList) bbGroupOpen[info.board] = info.isRoot;
    bbCount.textContent = "";
    bbEmpty.hidden = false;
    bbEmpty.textContent = "Waiting for values…";
}

// ------------------------------------------------------------------ //
// Sidebar — resizable and collapsible like the drawer; the camera works around it
// ------------------------------------------------------------------ //
// The shown width lives in --sidebar-width on <html>, so the drawer's left
// edge and the banner stack's centre follow it in CSS alone; the camera reads
// sidebarWidth() (visibleViewport). Collapsed, a strip as wide as the
// drawer's toolbar is tall keeps the fold button, as the collapsed drawer
// keeps its toolbar. See docs/architecture.md, "The sidebar".
const sidebar = document.getElementById("sidebar");
const SIDEBAR_DEFAULT = 320;                // px
const SIDEBAR_MIN = 200;                    // px; at most half the window
const SIDEBAR_STRIP = 49;                   // collapsed: 48 px plus the right border

// Right is wider; dragging a collapsed sidebar opens it at the dragged width.
// Resized or folded, the drawer under the canvas changed width with it, so
// the Timeline's bars are redrawn for its new axis.
const sidebarPane = KleinDrawer.pane({
    el: sidebar, handle: document.getElementById("sidebar-handle"),
    button: document.getElementById("sidebar-collapse"),
    key: "klein.sidebar", sizeName: "width", cssVar: "--sidebar-width", axis: "x", grow: 1,
    initial: SIDEBAR_DEFAULT, strip: () => SIDEBAR_STRIP,
    clamp: (w) => Math.round(Math.max(SIDEBAR_MIN, Math.min(w, window.innerWidth / 2))),
    labels: ["Collapse panel", "Expand panel"], ariaLabel: true,
    onResize: () => requestRender(),
    onFold: nudgeCamera,
});

// How much of the viewport's left edge the pane covers. The canvas spans the
// whole window, so this is what keeps the tree out from under the pane.
function sidebarWidth() {
    return sidebarPane.shown();
}

// Folded or unfolded: nudge the view by half the change so the tree stays
// centred in the space that is actually visible — without throwing away the
// reader's zoom/pan.
function nudgeCamera(before) {
    if (!rootNodeSnapshot) return;
    const current = d3.zoomTransform(svg.node());
    const shifted = d3.zoomIdentity
        .translate(current.x + (sidebarWidth() - before) / 2, current.y)
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
// the tree under the "Viewing t = …" pill and pausing doesn't move it. A
// second pill at once may overlap the tree's top for a moment.
const BANNER_INSET = 12 + 34 + 10;

function visibleViewport() {
    const left = sidebarWidth();
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
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
function cameraMs() {
    return reducedMotion.matches ? 0 : 500;
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
    KleinTimeline.revealRows(frontier.map(node => node.data.id));
}

let pulseTimer = null;

function pulseNodes(nodes) {
    const wanted = new Set(nodes);
    gContainer.selectAll(".node-rect.focused").classed("focused", false);
    const rects = gContainer.selectAll("g.node").filter(d => wanted.has(d)).select(".node-rect");
    rects.each(function() { void this.getBoundingClientRect(); });   // restart a pulse in flight
    rects.classed("focused", true);
    clearTimeout(pulseTimer);
    pulseTimer = setTimeout(
        () => gContainer.selectAll(".node-rect.focused").classed("focused", false), 1100);
}

// Hovering a board row says which card it belongs to, without moving anything.
function highlightNode(node, on) {
    gContainer.selectAll("g.node").filter(d => d === node)
        .select(".node-rect").classed("highlight", on);
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
        if (rootNodeSnapshot) {
            updateTreeLayout(rootNodeSnapshot);
            resetCamera();
        }
    });
}

// ------------------------------------------------------------------ //
// Banners — one stack of pills along the top of the canvas
// ------------------------------------------------------------------ //
// A banner is addressed by a stable `key`, so repeated news about the same
// thing replaces it instead of piling up, and any caller can take its own
// message down without knowing what else is on screen. `kind` picks the dot
// colour and the sort order (see .banner.* in styles.css); `timeout` makes it
// clear itself. A new kind of message costs one showBanner() call.
//
// `action` ({label, onClick}) adds a button, as "Back to live" on the
// "Viewing t = …" pill; `mono` sets the text in monospace; `live: false`
// keeps a pill that changes on every key press out of the aria-live stack's
// announcements.
const bannerStack = document.getElementById("banner-stack");
const BANNER_FADE_MS = 450;     // must match the .banner.leaving transition
const banners = new Map();      // key -> { el, text, button, timers }

function showBanner(key, text, { kind = "info", timeout = 0, action = null, mono = false,
                                 live = true } = {}) {
    let entry = banners.get(key);
    if (!entry) {
        const el = bannerStack.appendChild(document.createElement("div"));
        el.dataset.key = key;
        if (!live) el.setAttribute("aria-live", "off");
        const textEl = el.appendChild(document.createElement("span"));
        textEl.className = "banner-text" + (mono ? " mono" : "");
        let button = null;
        if (action) {
            button = el.appendChild(document.createElement("button"));
            button.type = "button";
            button.className = "banner-action";
            button.textContent = action.label;
            button.addEventListener("click", action.onClick);
        }
        entry = { el, text: textEl, button, timers: [] };
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
// `status` and `blackboard` frames. Either way the same
// paintStatus/renderBlackboards draw it; only the source differs.
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
KleinDrawer.onSeek(seek);
KleinTimeline.connect({
    seek,
    toggleFold: (id) => { const d = nodeById(id); if (d) toggleFold(d); },
    foldOthers: (id) => { const d = nodeById(id); if (d) foldOthers(d); },
    glyphFor,
});

// The latest frames, for when there is no recording to show.
let frameTree = null;
let frameStatus = {};
let frameBoards = null;

// The root id of the tree on the canvas. Node ids are generation-prefixed (see
// updateTreeLayout), so an equal id is the same tree: a `segment` and a `layout`
// frame for one tree draw it once, and a same-tree restart not at all.
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
    (document.hidden ? setTimeout : requestAnimationFrame)(render);
}

// A new tree: rebuild the hierarchy, the board list and the camera.
function showTree(treeData) {
    drawnTreeId = treeData.id;
    rootNodeSnapshot = d3.hierarchy(treeData);
    rootNodeSnapshot.x0 = 0;
    rootNodeSnapshot.y0 = 0;
    tagSubtreeDepth(rootNodeSnapshot);

    bbBoardList = collectBoards(rootNodeSnapshot);
    lastStatusMap = {};      // its uids indexed the tree we just dropped
    lastActiveStatusMap = {};
    resetBlackboards();
    updateTreeLayout(rootNodeSnapshot);
    resetCamera();
    boardsDirty = true;
}

function render() {
    renderQueued = false;
    const rec = shownRecording();
    KleinDrawer.showRecording(rec, recordingSupport);
    const now = performance.now();
    // Eviction dropped the moment shown: pause on the oldest kept record (just
    // after it, as clicking its Log row does), and say so rather than jump
    // silently. Eviction goes oldest first, so that is the first segment's.
    if (rec && rec.backfillDone && KleinCursor.evicted(clock, now, rec)) {
        const first = rec.segments[0];
        clock = KleinCursor.pause(first.id, Math.min(first.startSeq + 1, first.headSeq));
        showBanner("evicted", `Older than the kept ${KleinDrawer.keptSpan(rec)}: `
                   + "moved to the oldest kept transition", { kind: "info", timeout: 6000 });
    }
    let pos = rec && rec.backfillDone ? KleinCursor.cursorPos(clock, now, rec) : null;
    // Playing moves on every frame, and goes live once it reaches the head.
    if (pos && clock.mode === "playing") {
        if (pos.live) clock = KleinCursor.live();
        else requestRender({ boards: true });
    }
    const seg = pos && rec.segment(pos.seg);
    // An ended segment stays the source while the robot is away (its head is
    // the last state klein saw); once klein says the robot is back but can't
    // record, its status frames are shown instead.
    const recording = Boolean(seg)
        && !(pos.live && seg.tEnd !== null && recordingSupport === "unsupported");
    const source = recording ? "recording" : "frames";
    if (source !== displaySource) boardsDirty = true;
    displaySource = source;
    document.body.classList.toggle("viewing-past", recording && !pos.live);
    // An opened file without a .bb.jsonl sidecar: there is no blackboard at any moment.
    const noBoards = recording && rec.source === "file" && seg.bb.tStart === null;
    document.body.classList.toggle("no-blackboard", noBoards);
    // The moment shown, first in the banner stack, with the way back.
    if (recording && !pos.live) {
        // An opened file has no live to go back to: no button (Esc still
        // returns to its end).
        const action = rec.source === "file" ? null
            : { label: "Back to live", onClick: KleinDrawer.goLive };
        showBanner("viewing", `Viewing t = ${KleinDrawer.formatTime(pos.t)}`,
                   { kind: "past", mono: true, live: false, action });
    } else {
        hideBanner("viewing");
    }
    KleinDrawer.update(pos && rec, pos);

    const tree = recording ? seg.layout : frameTree;
    if (tree && tree.id !== drawnTreeId) showTree(tree);
    KleinTimeline.update(recording ? rec : null, pos, rootNodeSnapshot, clock);
    if (!rootNodeSnapshot) return;

    lastStatusMap = recording
        ? KleinRecording.decodeState(KleinRecording.stateAtSeq(seg, pos.seq), seg.uids)
        : frameStatus;
    if (Object.values(lastStatusMap).some(entry => entry.status === "RUNNING")) {
        lastActiveStatusMap = lastStatusMap;
    }
    paintStatus(lastStatusMap);

    if (!boardsDirty) return;
    boardsDirty = false;
    // Live takes the newest sample, even one stamped a moment after the last
    // drain's head. Null before a segment's first sample: live keeps what is
    // shown (or waits for values); a past moment shows no values, since what
    // is shown belongs to another moment.
    const boards = recording ? KleinRecording.bbAt(seg, pos.live ? Infinity : pos.t)
                             : frameBoards;
    // Flash only live changes: not moving through the past, and not the jump
    // back to live from it.
    const live = !recording || pos.live;
    if (boards) {
        renderBlackboards(boards, { flash: live && boardsLive });
    } else if (noBoards) {
        renderBlackboards({}, { flash: false, empty: "No blackboard in this file." });
    } else if (!live) {
        // Before the segment's kept blackboard: dropped by eviction (which
        // cuts the blackboard at the exact cutoff, transitions by whole
        // chunks), or a moment before its first sample.
        const empty = seg.bbDropped ? "Blackboard history from this moment was dropped."
                                    : "No blackboard sample yet at this moment.";
        renderBlackboards({}, { flash: false, empty });
    } else if (!boardsLive) {
        renderBlackboards({}, { flash: false, empty: "Waiting for values…" });
    }
    boardsLive = live;
}

// Read-only facts about the mirror and the display, for the test harness
// (BrowserProbe): `displayed` is the state last painted.
window.kleinDebug = () => {
    const rec = shownRecording();
    return { ...(rec ? KleinRecording.describe(rec) : {}),
             displaySource, displayed: lastStatusMap };
};

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
        frameTree = null;
        frameStatus = {};
        frameBoards = null;
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
            frameTree = message.data;
            frameStatus = {};        // its uids indexed the previous tree
            frameBoards = null;
            requestRender({ boards: true });
        }

        else if (message.type === "status") {
            frameStatus = message.data;
            requestRender();
        }

        else if (message.type === "blackboard") {
            frameBoards = message.data || {};
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

window.addEventListener("DOMContentLoaded", () => {
    fillLegendGlyphs();
    connectGatewayPipeline();
});
