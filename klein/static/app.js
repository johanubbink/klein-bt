let rootNodeSnapshot = null;

// The dashboard depends on two links: browser <-> klein (this WebSocket)
// and klein <-> robot (reported by klein). We track both so the UI can
// say precisely which one is down.
let gatewayConnected = false;
let robotConnected = false;
let robotDetail = "Connecting to klein gateway…";

const nodeWidth = 220;
// Three text rows (type, name, ports) plus even ~7px gaps above, between and
// below them. Their ink is ~28px, so the card needs ~56px for the rows to sit
// evenly; at 50 the name ends up crowding the type line above it.
const nodeHeight = 56;
const cardPadX = 12;            // left/right text inset, shared by every row

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

function truncate(text, limit) {
    return text.length > limit ? text.slice(0, limit - 1) + "\u2026" : text;
}

// One port per line, under the node's own name, for the hover tooltip. A node
// with no ports gets no title at all rather than an empty box.
function portTitle(node) {
    const entries = Object.entries(node.ports || {});
    if (!entries.length) return "";
    const lines = entries.map(([key, value]) => `  ${key} = ${value}`);
    return [`${node.type} "${node.name}"`, ...lines].join("\n");
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
    vertical:   { nodeSize: [240, 130], depthStep: 130 },
    horizontal: { nodeSize: [76, 300],  depthStep: 280 },   // sibling spacing = card height + 20
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
        .attr("class", "node")
        .each(d => { d._statusKey = null; })   // (re)appeared: force next status frame to repaint it
        .attr("transform", nodeTransform(sourceNode.x0 || 0, sourceNode.y0 || 0))
        .on("click", (event, d) => {
            if (event.defaultPrevented) return;
            if (d.children) {
                d._children = d.children;
                d.children = null;
            } else {
                d.children = d._children;
                d._children = null;
            }
            updateTreeLayout(d);
        });

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

    // Type tag
    nodeEnter.append("text")
        .attr("class", "node-type")
        .attr("x", -nodeWidth / 2 + cardPadX)
        .attr("y", rowType)
        .text(d => d.data.type);

    // Display name (truncated)
    nodeEnter.append("text")
        .attr("class", "node-name")
        .attr("x", -nodeWidth / 2 + cardPadX)
        .attr("y", rowName)
        .text(d => d.data.name.length > 20 ? d.data.name.substring(0, 18) + "..." : d.data.name);

    // Ports the tree author wrote on this node, along the card's bottom edge —
    // what a Precondition actually tests, which case a Switch matched. Blank
    // for a node with no ports; the untruncated set is in the hover title.
    nodeEnter.append("text")
        .attr("class", "node-ports")
        .attr("x", -nodeWidth / 2 + cardPadX)
        .attr("y", rowPorts)
        .text(d => truncate(portSummary(d.data.ports), maxPortChars));

    // Native tooltip: the full port list, one per line, so a truncated summary
    // is always one hover away from being readable in full. A node with no
    // ports gets no <title>, and so no empty tooltip.
    nodeEnter.filter(d => portSummary(d.data.ports) !== "")
        .append("title")
        .text(d => portTitle(d.data));

    // UID label (blank when this node carries no UID)
    nodeEnter.append("text")
        .attr("class", "node-uid")
        .attr("x", nodeWidth / 2 - 55)
        .attr("y", rowType)
        .text(d => d.data.uid == null ? "" : `UID ${String(d.data.uid).padStart(3, '0')}`);

    // Status pill background
    nodeEnter.append("rect")
        .attr("class", "status-pill")
        .attr("x", nodeWidth / 2 - 75)
        .attr("y", rowName - 12)
        .attr("width", 65)
        .attr("height", 16)
        .attr("rx", 3)
        .style("fill", "var(--color-IDLE)");

    // Status text
    nodeEnter.append("text")
        .attr("class", "node-status-text")
        .attr("x", nodeWidth / 2 - 42)
        .attr("y", rowName)
        .attr("text-anchor", "middle")
        .text("IDLE");

    // Merge + animate to final positions
    nodeEnter.merge(nodeSelection).transition().duration(250)
        .attr("transform", d => nodeTransform(d.x, d.y));

    nodeSelection.exit().transition().duration(250)
        .attr("transform", nodeTransform(sourceNode.x, sourceNode.y))
        .remove();

    // Cache positions for the next transition's origin
    nodesList.forEach(d => {
        d.x0 = d.x;
        d.y0 = d.y;
    });
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
function applyStatus(telemetryMap) {
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

function renderBlackboards(boards) {
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
            if (stateKey in bbLastValues && bbLastValues[stateKey] !== serialized) {
                row.row.classList.remove("bb-changed");
                void row.row.offsetWidth;        // restart a flash already in flight
                row.row.classList.add("bb-changed");
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
    bbEmpty.textContent = "This robot reports no blackboards.";
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
// Sidebar — collapsible, and the camera works around it
// ------------------------------------------------------------------ //
const sidebar = document.getElementById("sidebar");
const sidebarCollapse = document.getElementById("sidebar-collapse");
const sidebarShow = document.getElementById("sidebar-show");

// How much of the viewport's left edge the pane covers. The canvas spans the
// whole window, so this is what keeps the tree out from under the pane.
function sidebarWidth() {
    return sidebar.classList.contains("collapsed") ? 0 : sidebar.offsetWidth;
}

function setSidebarOpen(open) {
    const width = sidebar.offsetWidth;
    sidebar.classList.toggle("collapsed", !open);
    sidebarShow.hidden = open;
    sidebarCollapse.setAttribute("aria-expanded", String(open));
    sidebarShow.setAttribute("aria-expanded", String(open));

    // Nudge the view by half the pane so the tree stays centred in the space
    // that is actually visible — without throwing away the reader's zoom/pan.
    if (!rootNodeSnapshot) return;
    const current = d3.zoomTransform(svg.node());
    const shifted = d3.zoomIdentity
        .translate(current.x + (open ? width / 2 : -width / 2), current.y)
        .scale(current.k);
    svg.transition().duration(220).call(zoomBehavior.transform, shifted);
}

sidebarCollapse.addEventListener("click", () => setSidebarOpen(false));
sidebarShow.addEventListener("click", () => setSidebarOpen(true));

// Pan the camera so the root sits at the conventional entry point:
// top-center for vertical trees, left-center for horizontal ones.
function resetCamera() {
    // Cards are center-anchored, so offset by half a card to keep the root
    // fully on-screen — and clear of the sidebar.
    const scale = 0.8;
    const left = sidebarWidth();
    const target = orientation === "vertical"
        ? d3.zoomIdentity.translate(left + (window.innerWidth - left) / 2, 80).scale(scale)
        : d3.zoomIdentity.translate(left + 40 + (nodeWidth / 2) * scale, window.innerHeight / 2).scale(scale);
    svg.transition().duration(500).call(zoomBehavior.transform, target);
}

// Fly the camera to one node and pulse its card — the blackboard panel's answer
// to "where is this subtree?". Collapsed ancestors are reopened first, since a
// board can belong to a subtree the reader has folded away.
function focusNode(node) {
    if (!node || !rootNodeSnapshot) return;

    let reopened = false;
    for (let ancestor = node.parent; ancestor; ancestor = ancestor.parent) {
        if (ancestor._children) {
            ancestor.children = ancestor._children;
            ancestor._children = null;
            reopened = true;
        }
    }
    if (reopened) updateTreeLayout(rootNodeSnapshot);   // node.x/y are set here

    const scale = 0.9;
    const [x, y] = orientation === "vertical" ? [node.x, node.y] : [node.y, node.x];
    const left = sidebarWidth();
    const centerX = left + (window.innerWidth - left) / 2;
    svg.transition().duration(500).call(
        zoomBehavior.transform,
        d3.zoomIdentity.translate(centerX - x * scale, window.innerHeight / 2 - y * scale).scale(scale)
    );
    pulseNode(node);
}

let pulseTimer = null;

function pulseNode(node) {
    gContainer.selectAll(".node-rect.focused").classed("focused", false);
    const rect = gContainer.selectAll("g.node").filter(d => d === node).select(".node-rect");
    void rect.node()?.getBoundingClientRect();      // restart a pulse in flight
    rect.classed("focused", true);
    clearTimeout(pulseTimer);
    pulseTimer = setTimeout(
        () => gContainer.selectAll(".node-rect.focused").classed("focused", false), 1100);
}

// Hovering a board row says which card it belongs to, without moving anything.
function highlightNode(node, on) {
    gContainer.selectAll("g.node").filter(d => d === node)
        .select(".node-rect").classed("highlight", on);
}

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

// Reflect both links: green when the robot is streaming, amber (plus a
// banner) when klein is up but the robot is unreachable, red when klein
// itself can't be reached.
function updateConnectionUI() {
    const dot = d3.select("#conn-dot");
    const txt = d3.select("#conn-text");
    const banner = d3.select("#robot-banner");

    if (!gatewayConnected) {
        dot.attr("class", "dot");
        txt.text("klein gateway offline — reconnecting…");
        banner.text("⚠  Lost connection to the klein gateway — reconnecting…")
              .attr("class", "visible");
    } else if (!robotConnected) {
        dot.attr("class", "dot warn");
        txt.text(robotDetail);
        banner.text("⚠  " + robotDetail).attr("class", "visible");
    } else {
        dot.attr("class", "dot online");
        txt.text(robotDetail);
        banner.attr("class", "");   // robot is streaming: hide the banner
    }
}

// ------------------------------------------------------------------ //
// Gateway WebSocket connection
// ------------------------------------------------------------------ //
function connectGatewayPipeline() {
    // Same origin as this page — klein serves HTTP + WebSocket on one port.
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);

    ws.onopen = () => {
        gatewayConnected = true;
        updateConnectionUI();       // klein sends the robot's state right after
    };

    ws.onclose = () => {
        gatewayConnected = false;
        robotConnected = false;     // with klein gone we no longer know the robot
        updateConnectionUI();
        setTimeout(connectGatewayPipeline, 2000);
    };

    ws.onerror = () => ws.close();

    ws.onmessage = (event) => {
        const message = JSON.parse(event.data);

        if (message.type === "layout") {
            const treeData = message.data;
            if (!treeData) return;

            rootNodeSnapshot = d3.hierarchy(treeData);
            rootNodeSnapshot.x0 = 0;
            rootNodeSnapshot.y0 = 0;

            bbBoardList = collectBoards(rootNodeSnapshot);
            resetBlackboards();
            updateTreeLayout(rootNodeSnapshot);
            resetCamera();
        }

        else if (message.type === "status" && rootNodeSnapshot) {
            applyStatus(message.data);
        }

        else if (message.type === "blackboard") {
            renderBlackboards(message.data || {});
        }

        else if (message.type === "robot") {
            robotConnected = message.connected;
            robotDetail = message.detail;
            updateConnectionUI();
        }
    };
}

window.addEventListener("DOMContentLoaded", connectGatewayPipeline);
