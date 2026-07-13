"""
Summarise the DLS structure tags published by an Omron NX PLC over EtherNet/IP.

Usage: python plc_summary.py <ip-address>
"""

import argparse
from collections import Counter

from aphyt import omron
from aphyt.cip.cip import CIPException
from aphyt.cip.cip_datatypes import CIPArray, CIPStructure

# Edit these lists to control what the script looks for.
STRUCTS = ["Seq", "Temp", "ILK", "Gauge", "PID", "V", "T", "S"]
INFO_TAGS = ["PLC_Name", "PLC_Status", "Epics_Num_dev"]

# Structure member whose non-empty entries mark instances actually in use.
USED_MARKER_MEMBER = "Desc"


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ip", help="PLC IP address")
    parser.add_argument(
        "-l",
        "--list",
        action="store_true",
        help="list every published user tag and its type "
        "(slow: walks the whole tag dictionary)",
    )
    args = parser.parse_args()

    with omron.NSeries(args.ip) as eip:
        print(f"PLC at {args.ip}")
        for tag in INFO_TAGS:
            try:
                print(f"  {tag} = {eip.read_variable(tag)}")
            except CIPException:
                print(f"  {tag}: not published")

        if args.list:
            list_all_tags(eip)
            return

        for name in STRUCTS:
            try:
                result = eip.read_variable(name)
            except CIPException:
                print(f"\n{name}: not published on this PLC")
                continue
            if isinstance(result, CIPStructure):
                summarise_struct(name, result)
            else:
                print(f"\n{name}: {type(result).__name__} = {result}")


if __name__ == "__main__":
    main()
