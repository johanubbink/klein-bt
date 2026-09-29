# Blackboards

The dashboard shows every blackboard the robot has, refreshed twice a second
while the tree runs.

## The list

The **Blackboards** section follows the tree. Boards appear in tree order,
nested ones indented under their parent, each labelled with its subtree name
and node UID. The root board opens on load; click the others to open them.

Hover a board to highlight its node in the tree. Click the `uid NN` badge to
jump to it; any collapsed subtree on the way opens up.

A row flashes amber when its value changes. Click a row to see a long value in
full, or a field-by-field breakdown for message types klein knows. A subtree
whose ports are all remapped to its parent has no values of its own; it stays
in the list, dimmed.

## How values are shown

Values arrive the way BehaviorTree.CPP serializes them: bools as `0`/`1`,
structs as JSON tagged with their type name. klein turns the ones it knows into
one readable line:

| blackboard value | shown as |
| --- | --- |
| `nav_msgs::msg::Path` | `151 poses · map · 12.4 m` |
| `geometry_msgs::msg::PoseStamped` | `map · x 3.20  y -0.75  yaw 90.0°` |
| `geometry_msgs::msg::Quaternion` | `yaw 90.0°` |
| `builtin_interfaces::msg::Duration` | `100 ms` |
| a double carrying `DBL_MAX` | `∞ (DBL_MAX)` |
| `1.2999999999999985` | `1.3` |

Numbers are rounded to six significant digits. Hover a value to see exactly
what the robot sent. Very large values, like a long path, are cut short for
display, with the full size noted.

`(not shown)` means the robot sent nothing for that entry. Either it was never
written, or its type has no JSON converter (a ROS node handle, a TF buffer, and
so on). klein can't tell which. To make a value show up, register a converter
with `BT::RegisterJsonDefinition<T>()`.

## Adding a renderer

Types klein doesn't know still show up, as the type name with its simple fields
inline, and expand to pretty-printed JSON. For a type you look at often, add an
entry to `REGISTRY` in [`klein/static/renderers.js`](../klein/static/renderers.js),
keyed on the `__type` string and returning `{summary, detail}`. Renderers are
plain functions, so one can reuse another (a wrapper type can call the renderer
for the type it wraps).
