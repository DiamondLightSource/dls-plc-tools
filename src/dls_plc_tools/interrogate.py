"""
Summarise the structure tags published by an Omron NX PLC over EtherNet/IP.

With only an IP address, walks the PLC's tag dictionary and summarises every
published structure it finds. See --help for tag listing, member detail and
live value watching.
"""

import time
from collections import Counter

from aphyt import omron
from aphyt.cip.cip import CIPException
from aphyt.cip.cip_datatypes import CIPArray, CIPStructure

from dls_plc_tools.structure import (
    ancestors,
    branch_paths,
    columns_for,
    dims_str,
    find,
    flatten,
    row_count,
    row_text,
    type_name,
)

# Tags printed as a header for every mode.
INFO_TAGS = ["PLC_Name", "PLC_Status", "Epics_Num_dev"]

# Structure member whose non-empty entries mark instances actually in use.
USED_MARKER_MEMBER = "Desc"

# Default columns for --detail; override per-run with --members.
WATCH_MEMBERS = ["Desc", "State", "Status", "Status_String", "ILK_Status"]

MAX_CELL_WIDTH = 24
CHANGED = "\x1b[1;33m"  # bold yellow
RESET = "\x1b[0m"
CLEAR = "\x1b[H\x1b[2J"


def summarise_struct(name, struct):
    print(f"\n{name}: {struct.variable_type_name}")

    members = struct.members
    # Members are grouped by shape rather than lumped together, because a
    # member that is not a CIP primitive is exactly what needs calling out.
    primitive_arrays = {
        m: v
        for m, v in members.items()
        if isinstance(v, CIPArray)
        and not isinstance(v.local_cip_data_type_object, CIPStructure)
    }
    struct_arrays = {
        m: v
        for m, v in members.items()
        if isinstance(v, CIPArray)
        and isinstance(v.local_cip_data_type_object, CIPStructure)
    }
    nested = {m: v for m, v in members.items() if isinstance(v, CIPStructure)}
    scalars = [
        m
        for m in members
        if m not in primitive_arrays and m not in struct_arrays and m not in nested
    ]

    counted = Counter(dims_str(v) for v in primitive_arrays.values()).most_common()
    for dims, count in counted:
        print(f"  {count} array members of ARRAY[{dims}]")
    for member, value in struct_arrays.items():
        print(f"  {member}: {type_name(value)}  (nested structure)")
    for member, value in nested.items():
        print(f"  {member}: {type_name(value)}  (nested structure)")
    if scalars:
        print(f"  scalar members: {', '.join(scalars)}")

    marker = primitive_arrays.get(USED_MARKER_MEMBER)
    if marker is not None:
        try:
            start = marker.start_array_elements[0]
            used = [i + start for i, v in enumerate(marker.value()) if str(v).strip()]
            print(
                f"  instances in use ({USED_MARKER_MEMBER} non-empty): "
                f"{len(used)} -> {used}"
            )
        except Exception as e:
            print(f"  (could not decode {USED_MARKER_MEMBER}: {e})")


def list_all_tags(eip):
    """Walk the PLC's full tag dictionary and print every published user tag."""
    eip.update_variable_dictionary()
    user_variables = eip.connected_cip_dispatcher.user_variables
    for name, dtype in sorted(user_variables.items()):
        if isinstance(dtype, CIPStructure):
            print(f"  {name}  {dtype.variable_type_name}")
        else:
            print(f"  {name}  {type(dtype).__name__}")
    system_count = len(eip.connected_cip_dispatcher.system_variables)
    print(f"\n{len(user_variables)} user tags ({system_count} system tags not shown)")


def show_struct(eip, struct):
    """Print every member of a structure with its type and array bounds.

    Nested structures are printed indented under their parent, with the dotted
    path that ``--members`` takes, so the output doubles as the column menu.
    """
    result = eip.read_variable(struct)
    if not isinstance(result, CIPStructure):
        print(f"{struct} is a {type(result).__name__}, not a structure")
        return
    print(f"\n{struct}: {result.variable_type_name}")
    members, _ = flatten(result, struct)
    _print_tree(members, depth=1)


def _print_tree(members, depth):
    for member in members:
        print(f"{'  ' * depth}{member.path}  {member.type_name}")
        _print_tree(member.children, depth + 1)


def _watch_columns(members, wanted, expand_all):
    """Pick the columns to show from a flattened structure.

    Selecting a nested path (``-m Ilk.Trip``) expands just enough of the tree to
    reach it; naming a branch itself (``-m Ilk``) leaves it collapsed, so the
    column holds the compact ``Trip=1 Msg=HiVac`` summary.
    """
    expanded = branch_paths(members) if expand_all else set()
    for path in wanted:
        expanded |= ancestors(path)
    columns = columns_for(members, expanded)
    if expand_all:
        # Every branch is expanded, so its summary column just repeats the
        # children beside it. The GUI keeps that column as the click target for
        # collapsing; a terminal has nothing to click, so drop it.
        return [column for column in columns if not column.member.branch]
    return [column for column in columns if column.member.path in wanted]


def watch_struct(eip, ip, struct, members, interval, expand_all=False):
    """Poll a structure and redraw a live table of the selected members.

    Reads the whole structure in one request sequence per refresh (read-only
    explicit messaging, the same mechanism the EPICS ether_ip driver uses),
    then picks the displayed members out client-side.
    """
    rows = None
    previous = {}
    while True:
        result = eip.read_variable(struct)
        if not isinstance(result, CIPStructure):
            print(f"{struct} is a {type(result).__name__}, not a structure")
            return
        # rows index into the value lists; the displayed index is row+start,
        # since PLC arrays are declared ARRAY[start..N] (commonly 1-based, with
        # index 0 as padding).
        tree, start = flatten(result, struct)
        columns = _watch_columns(tree, members, expand_all)
        if not columns:
            print(f"none of [{', '.join(members)}] exist in {struct}")
            print("pick some of its members with -m (or use --expand-all):")
            _print_tree(tree, depth=1)
            return

        if rows is None:
            marker = find(tree, USED_MARKER_MEMBER)
            if marker is not None and marker.values:
                rows = [i for i, v in enumerate(marker.values) if str(v).strip()]
            else:  # no marker member: show every row the structure has
                rows = list(range(row_count(tree)))

        cells = {}
        widths = {}
        for column in columns:
            path = column.member.path
            widths[path] = len(path)
            for idx in rows:
                text = row_text(column.member, idx)[:MAX_CELL_WIDTH]
                changed = previous.get((path, idx), text) != text
                previous[(path, idx)] = text
                cells[(path, idx)] = (text, changed)
                widths[path] = max(widths[path], len(text))

        paths = [column.member.path for column in columns]
        lines = [
            f"{struct} @ {ip}  {time.strftime('%H:%M:%S')}  "
            f"(every {interval}s, read-only)  Ctrl-C to stop",
            "",
            "  idx  " + "  ".join(p.ljust(widths[p]) for p in paths),
        ]
        for idx in rows:
            line = f"  {idx + start:>3}  "
            for path in paths:
                text, changed = cells[(path, idx)]
                padded = text.ljust(widths[path])
                if changed:
                    padded = f"{CHANGED}{padded}{RESET}"
                line += padded + "  "
            lines.append(line.rstrip())

        print(CLEAR + "\n".join(lines), flush=True)
        time.sleep(interval)


def read_plc(
    ip,
    list_tags=False,
    detail=None,
    show=None,
    members=None,
    interval=2.0,
    expand_all=False,
):
    """Connect to the PLC at ``ip`` and run one interrogation mode — the core of
    the ``dls-plc-tools read`` subcommand. With no mode selected, summarises every
    published structure (the dictionary walk)."""
    with omron.NSeries(ip) as eip:
        print(f"PLC at {ip}")
        for tag in INFO_TAGS:
            try:
                print(f"  {tag} = {eip.read_variable(tag)}")
            except CIPException:
                print(f"  {tag}: not published")

        if list_tags:
            list_all_tags(eip)
            return

        if show:
            try:
                show_struct(eip, show)
            except CIPException:
                print(f"{show}: not published on this PLC")
            return

        if detail:
            watch = members.split(",") if members else WATCH_MEMBERS
            watch_struct(
                eip,
                ip,
                detail,
                [w.strip() for w in watch if w.strip()],
                max(interval, 0.5),
                expand_all=expand_all,
            )
            return

        # Default mode: discover structure tags from the dictionary walk,
        # then read each one to summarise it.
        eip.update_variable_dictionary()
        user_variables = eip.connected_cip_dispatcher.user_variables
        structs = sorted(
            name
            for name, dtype in user_variables.items()
            if isinstance(dtype, CIPStructure)
        )
        print(f"\n{len(structs)} structure tags: {', '.join(structs)}")
        for name in structs:
            summarise_struct(name, eip.read_variable(name))
