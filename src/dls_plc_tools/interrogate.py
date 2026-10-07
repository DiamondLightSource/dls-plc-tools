"""
Summarise the structure tags published by an Omron NX PLC over EtherNet/IP.

With only an IP address, walks the PLC's tag dictionary and summarises every
published structure it finds. See --help for tag listing, member detail, live
value watching and diagnosing a tag that fails to decode.
"""

import asyncio
import time
from collections import Counter

from aphyt import omron
from aphyt.cip.cip import CIPCommonFormat, CIPException
from aphyt.cip.cip_datatypes import CIPArray, CIPStructure

from dls_plc_tools import aphyt_compat  # noqa: F401  (corrects aphyt on import)
from dls_plc_tools.layout import audit, slots
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


def _report_layout(eip, name):
    """Print the member layout audit for a tag whose read just failed.

    aphyt assigns the bytes it read onto the cached instance *before* decoding
    them, and that instance is the one in the dispatcher's dictionary, so the
    failed read is still inspectable here.
    """
    instance = eip.connected_cip_dispatcher.variables.get(name)
    if not isinstance(instance, CIPStructure):
        return
    for line in audit(name, instance):
        print(f"  {line}")


def _paged_read(eip, instance):
    """Repeat aphyt's own paged read of one tag, counting the bytes per chunk.

    ``_multi_message_variable_read`` advances its offset by the chunk size it
    asked for rather than by what came back, so a short chunk silently shortens
    the buffer it then decodes. Reading the same way here, but recording what
    each request actually returned, is what tells a short read apart from a
    member walk that drifted. Still read-only explicit messaging: the same
    service `read_variable` uses, one request in flight.
    """
    # The sync wrapper exposes no paged read, so this goes through the async
    # session it drives -- private, but the only way to see the chunk sizes.
    session = eip._instance  # noqa: SLF001
    max_read = session.MAXIMUM_LENGTH - 8
    chunks = []
    data = b""
    offset = 0
    while offset < instance.size:
        read_size = min(max_read, instance.size - offset)
        future = asyncio.run_coroutine_threadsafe(
            session._simple_data_segment_read(instance, offset, read_size),  # noqa: SLF001
            session.loop,
        )
        common = CIPCommonFormat()
        common.from_bytes(future.result().reply_data)
        chunks.append((offset, read_size, len(common.data)))
        data += common.data
        offset += max_read
    return data, chunks


def probe_struct(eip, name):
    """Diagnose a structure aphyt cannot decode, without the failing decode.

    Reads the tag byte-for-byte the way aphyt does, reports what came back,
    audits the member layout against it, then decodes each member separately so
    the one that actually fails is named — instead of a bare ``struct.error``
    from inside the library.
    """
    eip.update_variable_dictionary()
    instance = eip.connected_cip_dispatcher.variables.get(name)
    if instance is None:
        print(f"{name}: not published on this PLC")
        return
    if not isinstance(instance, CIPStructure):
        print(f"{name} is a {type(instance).__name__}, not a structure")
        return

    print(f"\nprobing {name}: {instance.variable_type_name}")
    data, chunks = _paged_read(eip, instance)
    print(f"  paged read of {instance.size} declared bytes in {len(chunks)} chunk(s):")
    for offset, asked, got in chunks:
        short = "  <-- short chunk" if got != asked else ""
        print(f"    offset {offset:>6}: asked {asked:>4}, got {got:>4}{short}")
    print(f"  total returned: {len(data)} bytes")

    instance.data = data
    for line in audit(name, instance):
        print(f"  {line}")

    print("  decoding each member on its own:")
    for slot in slots(instance):
        if "." in slot.path:  # nested members decode with their parent
            continue
        member = instance.members[slot.path]
        chunk = data[slot.offset : slot.end]
        member.data = chunk
        sizes = f"{len(chunk)} of {slot.size} bytes"
        try:
            member.value()
        except Exception as e:
            outcome = f"FAILED  {sizes}  ({type(e).__name__}: {e})"
        else:
            # A short slice that still decodes is worse than one that raises:
            # aphyt builds values out of whatever bytes it had, with no
            # complaint, so the member is quietly wrong.
            if len(chunk) < slot.size:
                outcome = f"SHORT   {sizes} — decoded from part of it"
            else:
                outcome = f"ok      {sizes}"
        print(f"    {slot.path:<28} {outcome}")


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
                # Always keep index 0 when it exists: ether_ip assumes base-0
                # arrays, even though Desc[0] is padding and so always blank.
                rows = [
                    i
                    for i, v in enumerate(marker.values)
                    if str(v).strip() or start + i == 0
                ]
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
    probe=None,
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

        if probe:
            probe_struct(eip, probe)
            return

        if show:
            try:
                show_struct(eip, show)
            except CIPException:
                print(f"{show}: not published on this PLC")
            except Exception as e:
                print(f"{show}: could not decode ({type(e).__name__}: {e})")
                _report_layout(eip, show)
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
            try:
                summarise_struct(name, eip.read_variable(name))
            except Exception as e:
                # One tag aphyt cannot decode used to abort the whole walk with
                # a traceback from inside the library, naming neither the tag
                # nor the member. Report it against the layout instead and keep
                # going -- the remaining tags are still worth having.
                print(f"\n{name}: could not decode ({type(e).__name__}: {e})")
                _report_layout(eip, name)
