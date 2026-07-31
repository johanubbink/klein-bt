let rootNodeSnapshot = null;

// The dashboard depends on two links: browser <-> klein (this WebSocket)
// and klein <-> robot (reported by klein). We track both so the UI can
// say precisely which one is down.
let gatewayConnected = false;
let robotConnected = false;
let robotDetail = "Connecting to klein gateway…";

const nodeWidth = 220;
const nodeHeight = 50;

// "vertical" is the standard BT convention: root at the top, children below,
// siblings ticked left-to-right. "horizontal" grows the tree rightward.
let orientation = "vertical";

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
    horizontal: { nodeSize: [70, 300],  depthStep: 280 },
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
        .attr("x", -nodeWidth / 2 + 12)
        .attr("y", -10)
        .text(d => d.data.type);

    // Display name (truncated)
    nodeEnter.append("text")
        .attr("class", "node-name")
        .attr("x", -nodeWidth / 2 + 12)
        .attr("y", 8)
        .text(d => d.data.name.length > 20 ? d.data.name.substring(0, 18) + "..." : d.data.name);

    // UID label (blank when this node carries no UID)
    nodeEnter.append("text")
        .attr("class", "node-uid")
        .attr("x", nodeWidth / 2 - 55)
        .attr("y", -10)
        .text(d => d.data.uid == null ? "" : `UID ${String(d.data.uid).padStart(3, '0')}`);

    // Status pill background
    nodeEnter.append("rect")
        .attr("class", "status-pill")
        .attr("x", nodeWidth / 2 - 75)
        .attr("y", 2)
        .attr("width", 65)
        .attr("height", 16)
        .attr("rx", 3)
        .style("fill", "var(--color-IDLE)");

    // Status text
    nodeEnter.append("text")
        .attr("class", "node-status-text")
        .attr("x", nodeWidth / 2 - 42)
        .attr("y", 13)
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
// Blackboards — one collapsible group per subtree, values live at 2 Hz
// ------------------------------------------------------------------ //
// Everything here starts collapsed: on a real tree the boards are far taller
// than the controls above them, and the panel floats over the canvas. The
// open/closed state and the last-seen values live outside the DOM so they
// survive every update frame.
const bbGroupOpen = {};     // board name -> is its body expanded?
const bbLastValues = {};    // "board key" -> last value, serialized, for the flash
const bbGroupEls = {};      // board name -> { group, body, count, rows: {key: row} }

const bbToggle = document.getElementById("bb-toggle");
const bbPanel = document.getElementById("bb-panel");
const bbGroups = document.getElementById("bb-groups");
const bbCount = document.getElementById("bb-count");
const bbEmpty = document.getElementById("bb-empty");

bbToggle.addEventListener("click", () => {
    const open = bbPanel.hidden;
    bbPanel.hidden = !open;
    bbToggle.classList.toggle("open", open);
    bbToggle.setAttribute("aria-expanded", String(open));
});

// BehaviorTree.CPP sends ints for bools (0/1), arrays for vectors, and objects
// tagged with "__type" for structs it has a JSON converter for.
//
// Null covers two cases the protocol can't tell apart: an entry declared but
// never written, and one holding a type with no JSON converter (a ROS node
// handle, a TF buffer, a chrono duration). On a real robot the second case is
// the common one, so the label says the value isn't shown rather than claiming
// it isn't set, and the tooltip explains how to make it visible.
const BB_NO_VALUE = "(not shown)";
const BB_NO_VALUE_HINT =
    "Either no value has been written, or its type has no JSON converter " +
    "(register one with BT::RegisterJsonDefinition<T>() to see it here).";

// Real blackboards carry values no side panel can show: a 150-pose nav path
// serializes to ~68 kB, which expands to a row tens of thousands of pixels tall
// and puts as much text in the DOM on every frame. Cap what we render and say
// how much was left out, so a huge value stays a readable sample of itself.
const BB_MAX_CHARS = 2000;

function formatBBValue(value) {
    if (value === null || value === undefined) return BB_NO_VALUE;
    const text = typeof value === "string" ? value : JSON.stringify(value);
    if (text.length <= BB_MAX_CHARS) return text;
    return `${text.slice(0, BB_MAX_CHARS)}… (${text.length.toLocaleString()} chars total)`;
}

function createBBGroup(name) {
    const group = document.createElement("div");
    group.className = "bb-group";

    const header = document.createElement("button");
    header.type = "button";
    header.className = "bb-group-header";
    header.setAttribute("aria-expanded", String(Boolean(bbGroupOpen[name])));

    const chevron = document.createElement("span");
    chevron.className = "bb-chevron";
    chevron.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.className = "bb-group-name";
    label.textContent = name;
    const count = document.createElement("span");
    count.className = "bb-group-count";
    header.append(chevron, label, count);

    const body = document.createElement("div");
    body.className = "bb-group-body";
    body.hidden = !bbGroupOpen[name];

    // Shown when the board holds nothing: a subtree whose ports are all
    // remapped to its parent has no entries of its own, and saying so beats
    // leaving a header with nothing under it.
    const empty = document.createElement("p");
    empty.className = "bb-group-empty";
    empty.textContent = "no local entries";
    body.appendChild(empty);

    header.addEventListener("click", () => {
        const open = !bbGroupOpen[name];
        bbGroupOpen[name] = open;
        body.hidden = !open;
        header.classList.toggle("open", open);
        header.setAttribute("aria-expanded", String(open));
    });
    header.classList.toggle("open", Boolean(bbGroupOpen[name]));

    group.append(header, body);
    bbGroups.appendChild(group);
    return { group, body, count, empty, rows: {} };
}

// Rows are buttons because they are disclosures too: clicking one unwraps a
// value too long for the panel's width.
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
    row.addEventListener("click", () => row.classList.toggle("expanded"));
    group.body.appendChild(row);
    return { row, value: valueEl, text: null };
}

// Reorder children only when the order is actually wrong: re-appending a row
// restarts its flash animation.
function syncBBOrder(container, ordered) {
    const correct = ordered.length === container.children.length
        && ordered.every((el, i) => container.children[i] === el);
    if (!correct) ordered.forEach(el => container.appendChild(el));
}

function renderBlackboards(boards) {
    // Board order comes from the gateway and follows the tree — root first,
    // then subtrees as they appear — so the panel reads like the canvas.
    const names = Object.keys(boards);

    for (const name of names) {
        const group = bbGroupEls[name] || (bbGroupEls[name] = createBBGroup(name));
        const entries = boards[name];
        // The robot's map order is arbitrary (it walks an unordered_map), so
        // sort to keep rows from reshuffling under the reader between frames.
        const keys = Object.keys(entries).sort();
        group.count.textContent = keys.length;
        group.empty.hidden = keys.length > 0;

        for (const key of keys) {
            const row = group.rows[key] || (group.rows[key] = createBBRow(group, key));
            const value = entries[key];
            const text = formatBBValue(value);
            if (row.text !== text) {
                const missing = value === null || value === undefined;
                row.value.textContent = text;
                row.value.title = missing ? BB_NO_VALUE_HINT : text;   // full value on hover
                row.value.classList.toggle("bb-unset", missing);
                row.text = text;
            }

            // Flash on a real change only — not the first time a key is seen.
            const stateKey = name + " " + key;
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
                delete group.rows[key];
                delete bbLastValues[name + " " + key];
            }
        }
        syncBBOrder(group.body, [group.empty, ...keys.map(key => group.rows[key].row)]);
    }

    for (const name of Object.keys(bbGroupEls)) {        // boards the robot dropped
        if (!(name in boards)) {
            bbGroupEls[name].group.remove();
            delete bbGroupEls[name];
        }
    }
    syncBBOrder(bbGroups, names.map(name => bbGroupEls[name].group));

    bbCount.textContent = names.length ? ` (${names.length})` : "";
    // Per-group notes cover empty boards; this line is for having none at all.
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
    bbCount.textContent = "";
    bbEmpty.hidden = false;
    bbEmpty.textContent = "Waiting for values…";
}

// Pan the camera so the root sits at the conventional entry point:
// top-center for vertical trees, left-center for horizontal ones.
function resetCamera() {
    // Cards are center-anchored, so offset by half a card to keep the root
    // fully on-screen.
    const scale = 0.8;
    const target = orientation === "vertical"
        ? d3.zoomIdentity.translate(window.innerWidth / 2, 80).scale(scale)
        : d3.zoomIdentity.translate(80 + (nodeWidth / 2) * scale, window.innerHeight / 2).scale(scale);
    svg.transition().duration(500).call(zoomBehavior.transform, target);
}

// Orientation toggle — reflow the same hierarchy and re-aim the camera; the
// 250ms node/link transitions animate the change.
d3.select("#orientation-toggle").on("click", () => {
    orientation = orientation === "vertical" ? "horizontal" : "vertical";
    d3.select("#orientation-toggle").text(`Layout: ${orientation}`);
    if (rootNodeSnapshot) {
        updateTreeLayout(rootNodeSnapshot);
        resetCamera();
    }
});

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
