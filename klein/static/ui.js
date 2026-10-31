// ui.js — the dashboard's toolkit: the DOM helpers every view uses (a
// pointer drag, a panel dragged to size and folded to a strip, writes that
// skip what is unchanged, keyed reconciliation of a list, a restarted
// animation, the reader's reduced-motion preference) and the words times,
// spans, counts and sizes are written in. Generic: it knows no view and
// keeps no state of its own. The drawer, its Log and transport, the
// Timeline, the overview, the tree, the blackboard panel and app.js import
// it; it imports nothing.
// See docs/architecture.md, "The sidebar" and "The drawer".

// ------------------------------------------------------------------ //
// Pointer drags and panels
// ------------------------------------------------------------------ //
// Follow a press on el (its pointerdown event) to its release: move(e) on
// each pointermove, then end(e) on pointerup or pointercancel. Pointer
// capture keeps the drag going when the pointer leaves el. The panels'
// edges, the Timeline's ruler and lanes, and the overview drag with it.
function drag(el, event, move, end) {
    el.setPointerCapture(event.pointerId);
    const done = (e) => {
        el.removeEventListener("pointermove", move);
        el.removeEventListener("pointerup", done);
        el.removeEventListener("pointercancel", done);
        end(e);
    };
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", done);
    el.addEventListener("pointercancel", done);
}

// Drag a panel's edge along one axis ("x" or "y"): onMove(from, delta) with
// the panel's size() at the press and the pointer's offset since, then
// onEnd() on release. An unmoved press (half of a double-click) changes
// nothing, so it can't expand a collapsed panel to its minimum.
function dragEdge(edge, axis, size, onMove, onEnd) {
    const coord = axis === "x" ? "clientX" : "clientY";
    edge.addEventListener("pointerdown", (event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        const start = event[coord];
        const from = size();
        drag(edge, event, (e) => { if (e[coord] !== start) onMove(from, e[coord] - start); }, () => onEnd());
    });
}

// A mouse click on a button inside el lets go of the focus, so the keys
// (Space above all, R/F) work right away; a key press keeps it there.
function blurOnClick(el) {
    el.addEventListener("click", (event) => {
        const button = event.target.closest("button");
        if (button && event.detail) button.blur();
    });
}

// A panel dragged to size on one edge and folded to a strip by its button or
// a double-click on that edge, remembered in localStorage: the drawer
// (drawer.js) and the sidebar (app.js). Its shown size goes in a custom
// property on <html>, so whatever sits beside it follows in CSS alone.
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
    button.addEventListener("click", toggle);
    blurOnClick(button);
    window.addEventListener("resize", apply);   // the limits follow the window
    apply();
    return { shown, collapsed: () => collapsed };
}

// ------------------------------------------------------------------ //
// Writes
// ------------------------------------------------------------------ //
// The views paint on every render, so these write only what changed: a new
// text node or attribute on every frame would have the browser lay the page
// out again each time.
function setText(el, text) {
    if (el.textContent !== text) el.textContent = text;
}

function setAttr(el, name, value) {
    if (el.getAttribute(name) !== value) el.setAttribute(name, value);
}

function setStyle(el, name, value) {
    if (el.style[name] !== value) el.style[name] = value;
}

// Add cls to el, restarting its animation if one is in flight.
function restartClass(el, cls) {
    el.classList.remove(cls);
    void el.getBoundingClientRect();
    el.classList.add(cls);
}

// Reconcile `container`'s children with `keys`, in that order, through
// `cache`: key -> its entry, whose `nodes` are its elements. A new key's
// entry is create(key), its nodes appended; every key's entry is then
// update(entry, key); a key gone has its nodes removed. Children are
// reordered only when the order is actually wrong: re-appending a node
// restarts its animation (a row's fade). The blackboard panel's boards and
// their rows use it.
function syncKeyed(container, keys, cache, create, update) {
    const wanted = new Set(keys);
    for (const key of keys) {
        let entry = cache[key];
        if (!Object.hasOwn(cache, key)) {
            entry = cache[key] = create(key);
            container.append(...entry.nodes);
        }
        update(entry, key);
    }
    for (const key of Object.keys(cache)) {
        if (wanted.has(key)) continue;
        for (const node of cache[key].nodes) node.remove();
        delete cache[key];
    }
    const ordered = keys.flatMap(key => cache[key].nodes);
    const correct = ordered.length === container.children.length
        && ordered.every((el, i) => container.children[i] === el);
    if (!correct) container.append(...ordered);
}

// Whether the reader asked the system for reduced motion: the camera then
// moves at once, and nothing on the blackboard fades or pulses.
const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
function reducedMotion() {
    return motion.matches;
}

// ------------------------------------------------------------------ //
// Words
// ------------------------------------------------------------------ //
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

// A transition's time in the Log and the transport's clock: "14:03:22.201 030".
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

// The Timeline's window length: "10 s", "500 ms", "1.25 s", "200 µs", "10 min".
function formatWindow(us) {
    const short = (v) => String(Number(v.toPrecision(3)));
    if (us < 1e3) return `${Math.round(us)} µs`;
    if (us < 1e6) return `${short(us / 1e3)} ms`;
    if (us < 6e7) return `${short(us / 1e6)} s`;
    return `${short(us / 6e7)} min`;
}

// How much is kept, as the recorder pill says it: "42 s", "10 min", "1 h 5 min".
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

export {
    drag, dragEdge, blurOnClick, pane, setText, setAttr, setStyle, restartClass, syncKeyed,
    reducedMotion, formatClock, formatTime, formatDelta, formatWindow, formatKept, formatCount,
    formatBytes,
};
