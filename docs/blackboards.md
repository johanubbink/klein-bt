# Blackboards

A behavior tree's blackboard is where its state lives, so the dashboard shows
every board the robot owns, refreshed at 2 Hz while the tree runs. This page is
about reading them; how the values get there is in
[architecture.md](architecture.md) and [protocol.md](protocol.md).

## The list

The pane's **Blackboards** section lists every subtree's board. The list mirrors
the tree: boards appear in tree order, nested ones indented under their parent,
each named by its subtree and badged with that node's UID. The root board — the
mission's own state — is expanded on load; the others open on click.

Because each board is bound to a node, the panel and the canvas stay connected:
hovering a board highlights its card in the tree, and clicking the `uid NN` badge
flies the camera to it, reopening any subtree you had collapsed on the way.

A row flashes amber when its value changes. Clicking a row unwraps a long value
and, for a message klein has a renderer for, reveals a labelled breakdown of its
fields. A subtree whose ports are all remapped to its parent owns no values; its
row stays, dimmed, so the list still matches the tree.

## How values are shown

Values arrive as BehaviorTree.CPP serializes them — bools as `0`/`1`, structs as
JSON tagged with the registered type name — and klein renders the ones it
recognizes as a single readable line:

| blackboard value | shown as |
| --- | --- |
| `nav_msgs::msg::Path` | `151 poses · map · 12.4 m` |
| `geometry_msgs::msg::PoseStamped` | `map · x 3.20  y -0.75  yaw 90.0°` |
| `geometry_msgs::msg::Quaternion` | `yaw 90.0°` |
| `builtin_interfaces::msg::Duration` | `100 ms` |
| a double carrying `DBL_MAX` | `∞ (DBL_MAX)` |
| `1.2999999999999985` | `1.3` |

Numbers are trimmed to six significant digits so float noise doesn't fill the
row; hovering any value shows exactly what the robot sent. Very large values (a
multi-hundred-pose path serializes to tens of kilobytes) are capped for display
with their full size noted.

An entry reads `(not shown)` when the robot sent no value for it — either nothing
was ever written, or its type has no JSON converter (a ROS node handle, a TF
buffer, a timeout). The protocol can't distinguish the two; registering a
converter with `BT::RegisterJsonDefinition<T>()` makes such a value visible.

## Adding a renderer

Message types klein doesn't recognize still render — the type name with its
scalar fields inline, expanding to pretty-printed JSON — but a type you look at
often deserves better. Add one entry to `REGISTRY` in
[`klein/static/renderers.js`](../klein/static/renderers.js), keyed on the
`__type` string, returning `{summary, detail}`. Renderers are pure functions and
compose, so a wrapper type can reuse the renderer for what it wraps.
