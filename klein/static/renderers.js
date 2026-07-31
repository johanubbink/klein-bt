// klein.renderers — how a blackboard value is turned into something readable.
//
// BehaviorTree.CPP serializes a registered struct to JSON and tags it with
// "__type". That is faithful but unreadable: a nav_msgs::msg::Path arrives as
// tens of kilobytes of nested objects, and a plain double arrives with its full
// binary noise (1.2999999999999985). A renderer collapses one type into a single
// line, plus the fields worth naming when the row is opened.
//
// Adding a type is one entry in REGISTRY: key on the "__type" string, return
// { summary, detail }. `detail` is a list of [label, text] pairs — a null label
// means the text is a full-width block. Renderers compose freely (Path reuses
// Pose), and every one of them is a pure function of its value.

const KleinRenderers = (() => {
    "use strict";

    const DEG = 180 / Math.PI;

    // Null covers two cases the protocol cannot tell apart: an entry declared
    // but never written, and one holding a type with no JSON converter (a ROS
    // node handle, a TF buffer, a chrono duration). On a real robot the second
    // is the common one, so the label says the value isn't shown rather than
    // claiming it isn't set.
    const NO_VALUE = "(not shown)";
    const NO_VALUE_HINT =
        "Either no value has been written, or its type has no JSON converter " +
        "(register one with BT::RegisterJsonDefinition<T>() to see it here).";

    // Real blackboards carry values no side panel can show: a 150-pose path
    // serializes to ~68 kB, which expands to a row tens of thousands of pixels
    // tall and puts as much text in the DOM on every frame. Cap what we render
    // and say how much was left out.
    const MAX_CHARS = 2000;

    function truncate(text) {
        if (text.length <= MAX_CHARS) return text;
        return `${text.slice(0, MAX_CHARS)}… (${text.length.toLocaleString()} chars total)`;
    }

    // ---------------------------------------------------------------- //
    // Numbers
    // ---------------------------------------------------------------- //
    // Six significant digits is enough for anything a robot reports and short
    // enough to fit a panel; it also erases the float noise that makes two
    // equal-looking values differ (0.24999999999999925 -> 0.25). The exact
    // value is never lost — the row's tooltip carries it verbatim.
    function formatNumber(value) {
        // A field the robot's converter left out reads as absent, not "undefined".
        if (value === null || value === undefined) return "—";
        if (typeof value !== "number" || !Number.isFinite(value)) return String(value);
        // BT.CPP ports commonly initialise a "no limit" double to DBL_MAX;
        // printed in full it is pure noise, and it must be caught before the
        // exponential branch below.
        if (Math.abs(value) === Number.MAX_VALUE) return value > 0 ? "∞ (DBL_MAX)" : "-∞ (-DBL_MAX)";
        if (Number.isInteger(value)) return String(value);
        const magnitude = Math.abs(value);
        if (magnitude >= 1e6 || magnitude < 1e-4) return value.toExponential(3);
        return String(Number(value.toPrecision(6)));
    }

    // Summaries line values up in a column, so metres get a fixed width; the
    // detail block falls back to full precision.
    function metres(value) {
        if (typeof value === "number" && Number.isFinite(value)) return value.toFixed(2);
        return formatNumber(value);
    }

    function degrees(value) {
        return value === null ? "—" : `${value.toFixed(1)}°`;
    }

    // ---------------------------------------------------------------- //
    // ROS building blocks
    // ---------------------------------------------------------------- //
    function yawDeg(q) {
        if (!q) return null;
        const { x = 0, y = 0, z = 0, w = 1 } = q;
        return Math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)) * DEG;
    }

    function pitchDeg(q) {
        if (!q) return null;
        const { x = 0, y = 0, z = 0, w = 1 } = q;
        return Math.asin(Math.max(-1, Math.min(1, 2 * (w * y - z * x)))) * DEG;
    }

    function rollDeg(q) {
        if (!q) return null;
        const { x = 0, y = 0, z = 0, w = 1 } = q;
        return Math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y)) * DEG;
    }

    function timeSeconds(stamp) {
        if (!stamp) return null;
        return (stamp.sec || 0) + (stamp.nanosec || 0) / 1e9;
    }

    function timeText(stamp) {
        const seconds = timeSeconds(stamp);
        return seconds === null ? "—" : `${seconds.toFixed(3)} s`;
    }

    function pointText(p) {
        if (!p) return "—";
        return `x ${metres(p.x)}  y ${metres(p.y)}  z ${metres(p.z)}`;
    }

    function poseText(pose) {
        if (!pose) return "—";
        const p = pose.position || {};
        return `x ${metres(p.x)}  y ${metres(p.y)}  yaw ${degrees(yawDeg(pose.orientation))}`;
    }

    function xyzDetail(v, unit = "") {
        const suffix = unit ? ` ${unit}` : "";
        return [
            ["x", formatNumber(v && v.x || 0) + suffix],
            ["y", formatNumber(v && v.y || 0) + suffix],
            ["z", formatNumber(v && v.z || 0) + suffix],
        ];
    }

    function orientationDetail(q) {
        return [
            ["yaw", degrees(yawDeg(q))],
            ["pitch", degrees(pitchDeg(q))],
            ["roll", degrees(rollDeg(q))],
            ["quaternion", q
                ? `x ${formatNumber(q.x || 0)}  y ${formatNumber(q.y || 0)}  ` +
                  `z ${formatNumber(q.z || 0)}  w ${formatNumber(q.w === undefined ? 1 : q.w)}`
                : "—"],
        ];
    }

    function poseDetail(pose) {
        const p = (pose && pose.position) || {};
        return [
            ["x", `${formatNumber(p.x || 0)} m`],
            ["y", `${formatNumber(p.y || 0)} m`],
            ["z", `${formatNumber(p.z || 0)} m`],
            ...orientationDetail(pose && pose.orientation),
        ];
    }

    // ---------------------------------------------------------------- //
    // The registry
    // ---------------------------------------------------------------- //
    const REGISTRY = {
        "builtin_interfaces::msg::Time": (v) => ({
            summary: timeText(v),
            detail: [["sec", formatNumber(v.sec || 0)], ["nanosec", formatNumber(v.nanosec || 0)]],
        }),

        "builtin_interfaces::msg::Duration": (v) => {
            const seconds = timeSeconds(v) || 0;
            return {
                // Sub-second durations (a BT tick period, a service timeout) are
                // the common case, and read better in milliseconds.
                summary: Math.abs(seconds) < 1 ? `${(seconds * 1000).toFixed(0)} ms` : `${seconds.toFixed(3)} s`,
                detail: [["sec", formatNumber(v.sec || 0)], ["nanosec", formatNumber(v.nanosec || 0)]],
            };
        },

        "std_msgs::msg::Header": (v) => ({
            summary: `${v.frame_id || "(no frame)"} @ ${timeText(v.stamp)}`,
            detail: [
                ["frame", v.frame_id || "—"],
                ["stamp", timeText(v.stamp)],
                ["sec", formatNumber((v.stamp && v.stamp.sec) || 0)],
                ["nanosec", formatNumber((v.stamp && v.stamp.nanosec) || 0)],
            ],
        }),

        "geometry_msgs::msg::Point": (v) => ({ summary: pointText(v), detail: xyzDetail(v, "m") }),
        "geometry_msgs::msg::Vector3": (v) => ({ summary: pointText(v), detail: xyzDetail(v) }),

        "geometry_msgs::msg::Quaternion": (v) => ({
            summary: `yaw ${degrees(yawDeg(v))}`,
            detail: orientationDetail(v),
        }),

        "geometry_msgs::msg::Pose": (v) => ({ summary: poseText(v), detail: poseDetail(v) }),

        "geometry_msgs::msg::PoseStamped": (v) => {
            const frame = (v.header && v.header.frame_id) || "(no frame)";
            return {
                summary: `${frame} · ${poseText(v.pose)}`,
                detail: [
                    ["frame", frame],
                    ["stamp", timeText(v.header && v.header.stamp)],
                    ...poseDetail(v.pose),
                ],
            };
        },

        "geometry_msgs::msg::Twist": (v) => {
            const l = v.linear || {}, a = v.angular || {};
            const speed = Math.hypot(l.x || 0, l.y || 0, l.z || 0);
            return {
                summary: `v ${speed.toFixed(2)} m/s · ω ${degrees((a.z || 0) * DEG)}/s`,
                detail: [
                    ["linear", `x ${formatNumber(l.x || 0)}  y ${formatNumber(l.y || 0)}  z ${formatNumber(l.z || 0)} m/s`],
                    ["angular", `x ${degrees((a.x || 0) * DEG)}  y ${degrees((a.y || 0) * DEG)}  z ${degrees((a.z || 0) * DEG)} /s`],
                    ["speed", `${formatNumber(speed)} m/s`],
                ],
            };
        },

        "nav_msgs::msg::Path": (v) => {
            const poses = Array.isArray(v.poses) ? v.poses : [];
            const frame = (v.header && v.header.frame_id) || "(no frame)";
            const length = pathLength(poses);
            const parts = [`${poses.length} pose${poses.length === 1 ? "" : "s"}`, frame];
            if (poses.length > 1) parts.push(`${length.toFixed(1)} m`);
            return {
                summary: parts.join(" · "),
                detail: [
                    ["frame", frame],
                    ["stamp", timeText(v.header && v.header.stamp)],
                    ["poses", String(poses.length)],
                    ["length", `${formatNumber(length)} m`],
                    ["start", poses.length ? poseText(poses[0].pose) : "—"],
                    ["end", poses.length ? poseText(poses[poses.length - 1].pose) : "—"],
                ],
            };
        },
    };

    function pathLength(poses) {
        let total = 0;
        for (let i = 1; i < poses.length; i++) {
            const a = (poses[i - 1].pose || {}).position || {};
            const b = (poses[i].pose || {}).position || {};
            total += Math.hypot((b.x || 0) - (a.x || 0), (b.y || 0) - (a.y || 0), (b.z || 0) - (a.z || 0));
        }
        return total;
    }

    // ---------------------------------------------------------------- //
    // Fallbacks — still better than a wrapped JSON blob
    // ---------------------------------------------------------------- //
    function shortType(type) {
        const i = type.lastIndexOf("::");
        return i === -1 ? type : type.slice(i + 2);
    }

    function jsonBlock(value) {
        return [[null, truncate(JSON.stringify(value, null, 2))]];
    }

    // A struct with no renderer: name the type, then inline whatever scalars it
    // carries, which for small messages is the whole story.
    function renderUnknownStruct(value) {
        const scalars = [];
        for (const [key, field] of Object.entries(value)) {
            if (key === "__type") continue;
            if (typeof field === "number") scalars.push(`${key} ${formatNumber(field)}`);
            else if (typeof field === "string") scalars.push(`${key} ${field}`);
            else if (typeof field === "boolean") scalars.push(`${key} ${field}`);
        }
        const name = shortType(value.__type);
        return {
            summary: truncate(scalars.length ? `${name}  ${scalars.join("  ")}` : name),
            detail: jsonBlock(value),
        };
    }

    function renderArray(value) {
        const inline = value.map(item =>
            typeof item === "number" ? formatNumber(item)
                : typeof item === "string" ? item
                    : JSON.stringify(item));
        return {
            summary: truncate(`[${inline.join(", ")}]`),
            // A short vector says everything in its summary; only unpack longer
            // or nested ones.
            detail: value.length > 4 || value.some(item => item !== null && typeof item === "object")
                ? jsonBlock(value) : [],
        };
    }

    // ---------------------------------------------------------------- //
    // Entry points
    // ---------------------------------------------------------------- //
    // Returns { summary, detail, missing } for any value the gateway can send.
    function renderValue(value) {
        if (value === null || value === undefined) {
            return { summary: NO_VALUE, detail: [], missing: true };
        }
        if (typeof value === "number") return { summary: formatNumber(value), detail: [] };
        if (typeof value === "string" || typeof value === "boolean") {
            return { summary: String(value), detail: [] };
        }
        if (Array.isArray(value)) return renderArray(value);
        if (typeof value !== "object") return { summary: String(value), detail: [] };

        const renderer = REGISTRY[value.__type];
        if (renderer) {
            try {
                return renderer(value);
            } catch (error) {
                // A message missing the fields its type promises must not blank
                // the panel; fall through to showing it raw.
                console.warn(`klein: ${value.__type} renderer failed`, error);
            }
        }
        if (typeof value.__type === "string") return renderUnknownStruct(value);
        return { summary: truncate(JSON.stringify(value)), detail: jsonBlock(value) };
    }

    // The value exactly as the robot sent it, for the row's tooltip.
    function exactText(value) {
        if (value === null || value === undefined) return NO_VALUE_HINT;
        return truncate(typeof value === "string" ? value : JSON.stringify(value));
    }

    return {
        renderValue,
        exactText,
        formatNumber,
        NO_VALUE,
        NO_VALUE_HINT,
        MAX_CHARS,
        REGISTRY,
    };
})();
