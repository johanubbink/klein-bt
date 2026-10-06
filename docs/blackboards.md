# Blackboards

The dashboard shows every blackboard the robot has, refreshed twice a second
while the tree runs.

## The list

The **Blackboards** section follows the tree. Boards appear in tree order,
nested ones indented under their parent, each labelled with its subtree name
and node UID. The root board opens on load; click the others to open them.

Hover a board to highlight its node in the tree. Click the `uid NN` badge to
jump to it; any collapsed subtree on the way opens up.

A value that just changed gets a soft mark: a thin amber edge at the row's
left and the value tinted amber. While live or playing, the mark fades out
over about two seconds; paused on a moment (a Log row, the Timeline's
playhead), the keys that changed within the last sample (0.5 s) keep a steady
mark. A key that changes at nearly every sample (4 or more changes in 3 s,
such as a position) is streaming: it gets a steady dim edge and a `~` after
its value instead, so the panel stays calm. This holds with recording off
(`--record-buffer 0`) too, where the dashboard counts the changes it sees.
Right after a tree swap or a reconnect, such keys briefly (about 1.5 s) look
freshly changed before they settle, since the count starts over. Going back
to live does not mark what changed while you looked at the past, and opening
a closed board does not replay a change that happened while it was closed.
With reduced motion turned on, nothing fades: a fresh change keeps the
steady mark.

Hover a key to see which nodes use it: the cards that write it get a solid
cyan outline, the ones that read it a dashed one, and the key is picked out
on their port lines. A node folded away is outlined on the folded card that
hides it. Hover a card to ring the rows of the keys it reads and writes.
When a key changes, the cards that write it get a small amber dot at their
corner, with the same rules as the row's mark: it fades while live or
playing, holds while paused, and stays off for a streaming key. Which node
writes what comes from the robot's port directions and BT.CPP's remapping
rules (scripts best-effort); a key in an autoremapped subtree is linked to
that subtree's own board (a robot's tree XML doesn't carry `_autoremap`).

Click a row to see a long value in full, or a field-by-field breakdown for
message types klein knows. A subtree whose ports are all remapped to its
parent has no values of its own; it stays in the list, dimmed.

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
