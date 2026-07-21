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


def dims_str(member):
    return ", ".join(
        f"{s}..{s + n - 1}"
        for s, n in zip(
            member.start_array_elements, member.number_of_elements, strict=True
        )
    )


def summarise_struct(name, struct):
    print(f"\n{name}: {struct.variable_type_name}")

    arrays = {m: v for m, v in struct.members.items() if isinstance(v, CIPArray)}
    scalars = [m for m in struct.members if m not in arrays]

    for dims, count in Counter(dims_str(v) for v in arrays.values()).most_common():
        print(f"  {count} array members of ARRAY[{dims}]")
    if scalars:
        print(f"  scalar members: {', '.join(scalars)}")

    marker = arrays.get(USED_MARKER_MEMBER)
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


def member_values(struct_instance, member):
    """Return a member's contents as a list, or None if it is missing."""
    value = struct_instance.members.get(member)
    if value is None:
        return None
    if isinstance(value, CIPArray):
        return value.value()
    return [value.value()]  # scalar member: single-row column


def show_struct(eip, struct):
    """Print every member of a structure with its type and array bounds."""
    result = eip.read_variable(struct)
    if not isinstance(result, CIPStructure):
        print(f"{struct} is a {type(result).__name__}, not a structure")
        return
    print(f"\n{struct}: {result.variable_type_name}")
    for name, member in result.members.items():
        if isinstance(member, CIPArray):
            element = type(member.local_cip_data_type_object).__name__
            print(f"  {name}  ARRAY[{dims_str(member)}] OF {element}")
        else:
            print(f"  {name}  {type(member).__name__}")


def watch_struct(eip, ip, struct, members, interval):
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
        if all(m not in result.members for m in members):
            print(f"none of [{', '.join(members)}] exist in {struct}")
            print(f"pick some of its members with -m: {', '.join(result.members)}")
            return
        columns = {m: member_values(result, m) for m in members}
        # rows index into the value lists; displayed index is row+start, since PLC
        # arrays are declared ARRAY[start..N] (commonly 1-based, index 0 padding).
        start = next(
            (
                m.start_array_elements[0]
                for m in result.members.values()
                if isinstance(m, CIPArray)
            ),
            0,
        )

        if rows is None:
            labels = member_values(result, USED_MARKER_MEMBER)
            if labels:
                rows = [i for i, v in enumerate(labels) if str(v).strip()]
            else:  # no marker member: show every index of the first column
                first = next(v for v in columns.values() if v is not None)
                rows = list(range(len(first)))

        cells = {}
        widths = {}
        for member, values in columns.items():
            widths[member] = len(member)
            for idx in rows:
                if values is None or idx >= len(values):
                    text = "n/a"
                else:
                    text = str(values[idx]).strip()[:MAX_CELL_WIDTH]
                changed = previous.get((member, idx), text) != text
                previous[(member, idx)] = text
                cells[(member, idx)] = (text, changed)
                widths[member] = max(widths[member], len(text))

        lines = [
            f"{struct} @ {ip}  {time.strftime('%H:%M:%S')}  "
            f"(every {interval}s, read-only)  Ctrl-C to stop",
            "",
            "  idx  " + "  ".join(m.ljust(widths[m]) for m in members),
        ]
        for idx in rows:
            line = f"  {idx + start:>3}  "
            for member in members:
                text, changed = cells[(member, idx)]
                padded = text.ljust(widths[member])
                if changed:
                    padded = f"{CHANGED}{padded}{RESET}"
                line += padded + "  "
            lines.append(line.rstrip())

        print(CLEAR + "\n".join(lines), flush=True)
        time.sleep(interval)


def read_plc(ip, list_tags=False, detail=None, show=None, members=None, interval=2.0):
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
            watch_struct(eip, ip, detail, watch, max(interval, 0.5))
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
