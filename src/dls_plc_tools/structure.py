"""Flatten a CIP read result into displayable columns.

Most DLS structures are structure-of-arrays over CIP primitives, so a member
maps straight onto one table column. Some members are not primitives, and the
tag spreadsheet has plenty of them: a member can be an ``ARRAY[..] OF <struct>``
(``V.ILK[x]``, "Array [1..xxx] of EPICS Valve INTLK STRUCT"), a plain nested
struct, or a struct nested inside one of those (``Gauge[x].IMG.W``, which also
carries a 16-bit ``IMG.B`` array). aphyt decodes all of them, but a nested value
arrives as a ``CIPStructure`` whose ``repr`` is unreadable jammed into one cell::

    Ilk[1] ->  { type: ILK_Type | members: {'Trip': 1, 'Msg': HiVac} }

So a read result is turned into a tree of `Member` nodes. Leaves carry one value
per instance row; branches carry their children *and* a compact per-row summary,
so a branch can be shown collapsed in a single column and expanded into its
leaves on demand. Callers choose what is visible by passing a set of expanded
branch paths to `columns_for`.

An array of primitives nested *inside* a structure is deliberately a leaf: its
elements are joined into one cell rather than exploding into a column each,
because a single ``Gauge`` instance would otherwise contribute 16 bit columns.
Top-level array members are untouched — they are what defines the rows.
"""

from dataclasses import dataclass, field

from aphyt.cip.cip_datatypes import CIPArray, CIPStructure

# Joined nested-array cells are clipped to this. Scalars are never clipped: a
# STRING[39] description is the common case and must survive intact.
JOINED_MAX = 60
SUMMARY_MAX = 72

# CIP class name -> the type name the PLC and the DLS tag spreadsheet use.
# type(obj).__name__ alone gives "CIPInteger", which no PLC engineer reads.
_PLC_TYPE_NAMES = {
    "CIPBoolean": "BOOL",
    "CIPShortInteger": "SINT",
    "CIPInteger": "INT",
    "CIPDoubleInteger": "DINT",
    "CIPLongInteger": "LINT",
    "CIPUnsignedShortInteger": "USINT",
    "CIPUnsignedInteger": "UINT",
    "CIPUnsignedDoubleInteger": "UDINT",
    "CIPUnsignedLongInteger": "ULINT",
    "CIPReal": "REAL",
    "CIPLongReal": "LREAL",
    "CIPString": "STRING",
    "CIPByte": "BYTE",
    "CIPWord": "WORD",
    "CIPDoubleWord": "DWORD",
    "CIPLongWord": "LWORD",
    "CIPTime": "TIME",
    "CIPAbbreviatedStructure": "STRUCT",
}

COLLAPSED = "▸"  # right-pointing marker on a collapsed branch header
EXPANDED = "▾"  # down-pointing marker on an expanded branch header


def dims_str(member):
    """``ARRAY[1..256]`` bounds text for a CIPArray, as the PLC declares them."""
    return ", ".join(
        f"{s}..{s + n - 1}"
        for s, n in zip(
            member.start_array_elements, member.number_of_elements, strict=True
        )
    )


def type_name(obj):
    """PLC-facing type name for a CIP datatype instance.

    Structures report the type name the controller gave them (``ILK_Type``)
    rather than ``CIPStructure``, which is what makes a nested column's tooltip
    worth reading.
    """
    if isinstance(obj, CIPStructure):
        return obj.variable_type_name or "STRUCT"
    if isinstance(obj, CIPArray):
        element = obj.local_cip_data_type_object
        return f"ARRAY[{dims_str(obj)}] OF {type_name(element)}"
    return _PLC_TYPE_NAMES.get(type(obj).__name__, type(obj).__name__)


@dataclass
class Member:
    """One node of a flattened structure.

    Leaves hold ``values``: one entry per instance row. Branches instead hold
    ``children`` plus ``summary`` — also one entry per row — so the same node
    can be rendered either as a single collapsed column or as its leaves.
    """

    path: str
    type_name: str
    values: list | None = None
    branch: bool = False
    children: list["Member"] = field(default_factory=list)
    summary: list | None = None

    @property
    def label(self):
        """Just the last path segment, e.g. ``Trip`` of ``Ilk.Trip``."""
        return self.path.rpartition(".")[2]

    def paths(self):
        """This node's path and every descendant path, for filter matching."""
        yield self.path
        for child in self.children:
            yield from child.paths()


# --- building -----------------------------------------------------------


def flatten(result, name):
    """Turn a ``read_variable`` result into ``(members, start)``.

    ``start`` is the PLC array index of row 0 — DLS members are declared
    ``ARRAY[0..N]`` or ``ARRAY[1..N]``, so the displayed index is
    ``start + row``. Mirrors the three shapes a tag can take: a structure, a
    bare array, or a scalar.
    """
    if isinstance(result, CIPStructure):
        members = [_member(member, value) for member, value in result.members.items()]
        start = next(
            (
                value.start_array_elements[0]
                for value in result.members.values()
                if isinstance(value, CIPArray)
            ),
            0,
        )
        return members, start
    if isinstance(result, CIPArray):
        return [_member(name, result)], result.start_array_elements[0]
    # Scalar tag: read_variable already returned a native Python value, so
    # there is no CIP object to name the type from.
    return [Member(path=name, type_name=type(result).__name__, values=[result])], 0


def _member(path, obj):
    """Build the node for a member in *structure-of-arrays* context.

    That is the read tag itself, and any plain nested struct inside it: every
    array member is indexed by instance, so it becomes a column with one value
    per row.
    """
    if isinstance(obj, CIPArray):
        if isinstance(obj.local_cip_data_type_object, CIPStructure):
            # ARRAY OF <struct>: exactly one struct per instance row, so its
            # members flatten into columns that line up with the other members.
            return _instance_branch(path, type_name(obj), obj.value())
        return Member(path=path, type_name=type_name(obj), values=obj.value())
    if isinstance(obj, CIPStructure):
        # A plain nested struct is the same structure-of-arrays convention one
        # level down (PSU.Status.W[1]) — a grouping of per-instance members,
        # not one value. So recurse with these same rules and let its arrays
        # spread down the rows; flattening it onto row 0 would strand every
        # instance's value in a single comma-joined cell.
        # value() is what slices the struct's bytes into its members and
        # returns the struct itself; for an ARRAY OF <struct> the array's own
        # value() has already done that per element.
        return _struct_of_arrays_branch(path, obj.value())
    return Member(path=path, type_name=type_name(obj), values=[obj.value()])


def _struct_of_arrays_branch(path, struct):
    """Build a branch for a plain nested struct, recursing with the
    structure-of-arrays rules so its array members stay instance-indexed."""
    node = Member(path=path, type_name=type_name(struct), branch=True)
    node.children = [
        _member(f"{path}.{name}", value) for name, value in struct.members.items()
    ]
    node.summary = [_summarise(node, row) for row in range(row_count(node.children))]
    return node


def _instance_branch(path, branch_type, structs):
    """Build a branch from one ``CIPStructure`` per instance row.

    ``structs`` may contain ``None`` where a row has no instance; every element
    shares a type, so the first real one supplies the member names.
    """
    node = Member(path=path, type_name=branch_type, branch=True)
    template = next((s for s in structs if isinstance(s, CIPStructure)), None)
    if template is None:
        node.summary = ["" for _ in structs]
        return node
    for child_name in template.members:
        child_objs = [
            s.members.get(child_name) if isinstance(s, CIPStructure) else None
            for s in structs
        ]
        node.children.append(_child(f"{path}.{child_name}", child_objs))
    node.summary = [_summarise(node, row) for row in range(len(structs))]
    return node


def _child(path, objs):
    """Build the node for a member held once per instance row.

    ``objs[row]`` is that row's CIP object for this member, or ``None``.
    """
    template = next((o for o in objs if o is not None), None)
    if isinstance(template, CIPStructure):
        # Still one struct per row, so keep the per-instance rules — arrays
        # under here (Gauge[x].IMG.B) are a gauge's own bits, not instances.
        return _instance_branch(path, type_name(template), objs)
    if isinstance(template, CIPArray) and isinstance(
        template.local_cip_data_type_object, CIPStructure
    ):
        # An array of structs *inside* an instance: many structs on one row, so
        # there is no single column to expand it into. Summarise each element
        # instead and leave it as a leaf.
        return Member(
            path=path,
            type_name=type_name(template),
            values=[_struct_list_text(o) for o in objs],
        )
    return Member(
        path=path,
        type_name=type_name(template),
        # A CIPArray's value() is a list, which cell_text joins into one cell.
        values=[o.value() if o is not None else None for o in objs],
    )


# --- rendering ----------------------------------------------------------


def cell_text(value):
    """Render one leaf value as table-cell text.

    Lists (a nested array of primitives, or a multi-dimensional top-level one)
    are joined into a single cell. An all-boolean list becomes a bit string in
    groups of eight, index 0 leftmost, which is how a status word reads.
    """
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, bytes | bytearray):
        # BYTE/WORD/DWORD/LWORD come back from aphyt as raw bytes, so a status
        # word such as V.ILK_Status renders as b'A\x00' unless it is formatted.
        # Use the 16#0041 notation the PLC and the tag spreadsheet write.
        return f"16#{int.from_bytes(value, 'little'):0{len(value) * 2}X}"
    if isinstance(value, list):
        flat = list(_iter_flat(value))
        if flat and all(isinstance(v, bool) for v in flat):
            bits = "".join("1" if v else "0" for v in flat)
            text = " ".join(bits[i : i + 8] for i in range(0, len(bits), 8))
        else:
            text = ",".join(cell_text(v) for v in flat)
        return _clip(text, JOINED_MAX)
    return str(value).strip()


def row_text(member, row):
    """Cell text for ``member`` at instance ``row`` — its summary if it is a
    branch being shown collapsed, otherwise its value."""
    values = member.summary if member.branch else member.values
    if values is None or row >= len(values):
        return "n/a"
    return values[row] if member.branch else cell_text(values[row])


def row_count(members):
    """Number of instance rows: the longest column anywhere in the tree."""
    counts = [0]
    for member in members:
        values = member.summary if member.branch else member.values
        counts.append(len(values or []))
        if member.branch:
            counts.append(row_count(member.children))
    return max(counts)


@dataclass
class Column:
    """A column to display: which node supplies the values, the header text,
    and the branch path a click on that header should expand or collapse
    (``None`` for a plain leaf)."""

    member: Member
    header: str
    toggle: str | None = None


def columns_for(members, expanded=()):
    """The columns to display, given the set of expanded branch paths.

    A branch always keeps its own summary column — collapsed it is the whole
    story, expanded it stays as the affordance for collapsing again and as a
    compact overview — and contributes its children only when expanded.
    """
    columns = []
    for member in members:
        if not member.branch:
            columns.append(Column(member=member, header=member.path))
        elif member.path in expanded:
            columns.append(
                Column(
                    member=member,
                    header=f"{EXPANDED} {member.path}",
                    toggle=member.path,
                )
            )
            columns.extend(columns_for(member.children, expanded))
        else:
            columns.append(
                Column(
                    member=member,
                    header=f"{COLLAPSED} {member.path}",
                    toggle=member.path,
                )
            )
    return columns


def branch_paths(members):
    """Every branch path in the tree — pass as ``expanded`` to flatten fully."""
    paths = set()
    for member in members:
        if member.branch:
            paths.add(member.path)
            paths |= branch_paths(member.children)
    return paths


def find(members, path):
    """The node at ``path``, or ``None``. Used to locate the in-use marker."""
    for member in members:
        if member.path == path:
            return member
        found = find(member.children, path)
        if found is not None:
            return found
    return None


def ancestors(path):
    """Branch paths that must be expanded for ``path`` to be visible."""
    parts = path.split(".")
    return {".".join(parts[:i]) for i in range(1, len(parts))}


# --- helpers ------------------------------------------------------------


def _summarise(node, row):
    """``Trip=1 Msg=HiVac`` for one row of a branch, for the collapsed column."""
    parts = []
    for child in node.children:
        text = row_text(child, row)
        if child.branch:  # brace a nested branch so the nesting stays visible
            text = "{" + text + "}"
        parts.append(f"{child.label}={text}")
    return _clip(" ".join(parts), SUMMARY_MAX)


def _struct_list_text(array):
    """``{Trip=1 Msg=Hi}, {Trip=0 Msg=Ok}`` for an array of structs on one row."""
    if array is None:
        return None
    elements = array.value()
    texts = []
    for element in elements:
        node = _instance_branch("", type_name(element), [element])
        texts.append("{" + (node.summary[0] if node.summary else "") + "}")
    return _clip(", ".join(texts), JOINED_MAX)


def _iter_flat(values):
    for value in values:
        if isinstance(value, list):
            yield from _iter_flat(value)
        else:
            yield value


def _clip(text, limit):
    return text if len(text) <= limit else text[: limit - 1] + "…"
