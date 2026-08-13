"""Audit how aphyt maps a structure's members onto the bytes it read.

A whole-tag read can die inside aphyt with a bare ``struct.error: unpack
requires a buffer of 2 bytes``, thrown from the *last* member it decodes. That
traceback names neither the tag nor the member, and the real fault is upstream:
either fewer bytes came back than the controller declared the structure to be,
or aphyt's member walk drifted so that a member ran off the end of the data.

``CIPStructure.value()`` lays members out by walking them in declaration order,
padding each to its own alignment, and slicing ``data[offset:offset + size]``.
That walk is reproduced here in `slots`, without touching the PLC, so a failed
read can be turned into a report saying which member overran and by how much
(`audit`). Nothing in here decodes values — it is purely about offsets.

The two size models a BOOL array can follow are checked explicitly, because
aphyt's own is arithmetically wrong for some lengths: it computes
``n // 8 (+2 if n % 16)``, which agrees with Omron's ``ceil(n / 16) * 2`` word
packing for 1..7, 16..23, 32..39 elements and disagrees everywhere else (11
elements -> 3 bytes rather than 2). A structure whose BOOL array trips that is
mis-sliced from that member onwards, which strands the tail of the read.
"""

from dataclasses import dataclass

from aphyt.cip.cip_datatypes import CIPArray, CIPBoolean, CIPStructure

from dls_plc_tools.structure import type_name


@dataclass
class Slot:
    """One member's byte range within its structure, as aphyt slices it."""

    path: str
    type_name: str
    offset: int
    size: int

    @property
    def end(self):
        return self.offset + self.size


def _advance(offset, member, previous_type):
    """aphyt's own padding rule, from ``CIPStructure.value()``.

    Kept byte-identical to the library, including the second branch only being
    reachable for a member that is already aligned or aligns to a single byte
    (a string, a SINT, a BYTE): there, a change of member type bumps an odd
    offset to the next even one.
    """
    alignment = member.alignment
    if alignment != 0 and offset % alignment != 0:
        return offset + (alignment - offset % alignment)
    if previous_type is not type(member) and offset % 2 != 0:
        return offset + 1
    return offset


def slots(struct, prefix=""):
    """Byte ranges of every member of ``struct``, nested members included.

    Offsets of nested members are absolute within the top-level structure —
    aphyt reaches them by re-slicing the parent's slice, so the arithmetic is
    the same, and an absolute offset is what compares against the bytes read.
    """
    found = []
    offset = 0
    previous_type = None
    for name, member in struct.members.items():
        offset = _advance(offset, member, previous_type)
        path = f"{prefix}{name}"
        found.append(Slot(path, type_name(member), offset, member.size))
        if isinstance(member, CIPStructure):
            for slot in slots(member, f"{path}."):
                found.append(
                    Slot(slot.path, slot.type_name, offset + slot.offset, slot.size)
                )
        previous_type = type(member)
        offset += member.size
    return found


def bool_array_sizes(member):
    """``(aphyt, word_packed)`` byte sizes for a BOOL array member.

    ``word_packed`` is Omron's layout: BOOL arrays occupy whole 16-bit words.
    Where the two disagree, aphyt is wrong and every later member is skewed.
    """
    elements = 1
    for number in member.number_of_elements:
        elements *= number
    return member.size, ((elements + 15) // 16) * 2


def bool_array_mismatches(struct, prefix=""):
    """Every BOOL array member whose aphyt size differs from Omron's packing."""
    bad = []
    for name, member in struct.members.items():
        path = f"{prefix}{name}"
        if isinstance(member, CIPArray) and isinstance(
            member.local_cip_data_type_object, CIPBoolean
        ):
            aphyt_size, packed = bool_array_sizes(member)
            if aphyt_size != packed:
                bad.append((path, aphyt_size, packed))
        elif isinstance(member, CIPStructure):
            bad.extend(bool_array_mismatches(member, f"{path}."))
    return bad


def audit(name, struct):
    """Lines explaining how ``struct``'s member layout fits the bytes read.

    Written for the case where the read has just raised: ``struct.data`` still
    holds whatever came back, because aphyt assigns it before decoding.
    """
    read = len(struct.data)
    lines = [
        f"{name}: {struct.variable_type_name}",
        f"  controller declares {struct.size} bytes, read returned {read} bytes",
    ]
    if read < struct.size:
        lines.append(
            f"  short read: {struct.size - read} bytes missing — the paged read "
            "in aphyt's _multi_message_variable_read did not return everything"
        )

    member_slots = slots(struct)
    top_level = [slot for slot in member_slots if "." not in slot.path]
    if top_level:
        span = top_level[-1].end
        lines.append(f"  members occupy {span} bytes under aphyt's layout")
        if span != struct.size:
            lines.append(
                f"  layout disagrees with the controller by {span - struct.size:+d} "
                "bytes — member offsets are skewed, so values are wrong from the "
                "first bad member onwards, not just at the end"
            )

    overrun = [slot for slot in member_slots if slot.end > read]
    if overrun:
        first = overrun[0]
        lines.append(
            f"  first member past the end of the data: {first.path} "
            f"({first.type_name}) at {first.offset}..{first.end}"
        )

    for path, aphyt_size, packed in bool_array_mismatches(struct):
        lines.append(
            f"  BOOL array {path}: aphyt sizes it {aphyt_size} bytes, Omron packs "
            f"it into {packed} — aphyt's array size arithmetic is wrong here"
        )

    lines.append("  member layout (offset..end, size):")
    for slot in member_slots:
        flag = "  <-- past end of data" if slot.end > read else ""
        lines.append(
            f"    {slot.path:<28} {slot.type_name:<28} "
            f"{slot.offset:>6}..{slot.end:<6} {slot.size:>5}{flag}"
        )
    return lines
