"""Synthetic CIP structures for testing nested-member handling.

No PLC is reachable from CI, so these build the shapes aphyt would hand back
for the nested types the DLS tag spreadsheet actually declares:

* ``Ilk``  -- ``ARRAY[1..3] OF ILK_Type``, i.e. "Array [1..xxx] of EPICS Valve
  INTLK STRUCT": one struct per instance row. Its ``IMG`` member is a struct
  holding a WORD and a 16-bit BOOL array, after ``Gauge[x].IMG.W`` /
  ``Gauge[x].IMG.B[y]`` -- those bits belong to *one* gauge, so they must stay
  joined in a cell rather than spreading down the rows.
* ``Status`` -- a plain nested struct whose own members are arrays: the
  structure-of-arrays convention one level down, after ``PSU.Status.W[1]``.
  Its arrays *are* indexed by instance and must become per-row columns.
* ``Desc`` / ``State`` -- ordinary primitive array members, so a test can check
  the flat case still behaves.

Byte layouts are laid out by hand to match what ``CIPStructure.value()`` slices
out, so nothing here needs a connection.
"""

import struct as _struct

from aphyt.cip.cip_datatypes import (
    CIPArray,
    CIPBoolean,
    CIPInteger,
    CIPReal,
    CIPString,
    CIPStructure,
    CIPWord,
)


def cip_string(size):
    """A fixed-width CIP string member, as Sysmac's STRING[n] resolves to."""
    value = CIPString()
    value.size = size
    return value


def string_bytes(text, size):
    return text.encode("utf-8").ljust(size, b"\0")


def make_img():
    """``IMG_Type { W: WORD; B: ARRAY[0..15] OF BOOL }`` -- a status word and
    its own bits, the ``Gauge[x].IMG`` shape. Lives inside one instance."""
    bits = CIPArray()
    bits.from_instance(CIPBoolean(), 2, 1, [16], [0])
    img = CIPStructure()
    img.variable_type_name = "IMG_Type"
    img.add_member("W", CIPWord())
    img.add_member("B", bits)
    img.size = 4
    return img


def make_ilk_type():
    """``ILK_Type { Trip: INT; Msg: STRING[8]; IMG: IMG_Type }`` -- 14 bytes."""
    ilk = CIPStructure()
    ilk.variable_type_name = "ILK_Type"
    ilk.add_member("Trip", CIPInteger())
    ilk.add_member("Msg", cip_string(8))
    ilk.add_member("IMG", make_img())
    ilk.size = 14
    return ilk


def ilk_bytes(trip, msg, word):
    # W and B are the same status word: B is its bit view.
    return (
        _struct.pack("<h", trip)
        + string_bytes(msg, 8)
        + _struct.pack("<H", word)
        + _struct.pack("<H", word)
    )


def make_status(*, instances, words):
    """``Status_Type { W: ARRAY OF WORD; Code: ARRAY OF INT }`` -- a plain
    nested struct that is itself a structure-of-arrays, so both its members are
    indexed by instance."""
    status = CIPStructure()
    status.variable_type_name = "Status_Type"
    word_array = CIPArray()
    word_array.from_instance(CIPWord(), 2, 1, [instances], [1])
    codes = CIPArray()
    codes.from_instance(CIPInteger(), 2, 1, [instances], [1])
    status.add_member("W", word_array)
    status.add_member("Code", codes)
    status.size = 4 * instances
    status.data = _struct.pack(f"<{instances}H", *words) + _struct.pack(
        f"<{instances}h", *range(instances)
    )
    return status


def make_struct(*, instances=3):
    """A DLS-style structure-of-arrays with both nested shapes present."""
    root = CIPStructure()
    root.variable_type_name = "Gauge_Type"

    descriptions = ["pump1", "valve2", ""][:instances]
    desc = CIPArray()
    desc.from_instance(cip_string(8), 8, 1, [instances], [1])
    desc.data = b"".join(string_bytes(d, 8) for d in descriptions)
    root.add_member("Desc", desc)

    states = [4, 7, 0][:instances]
    state = CIPArray()
    state.from_instance(CIPInteger(), 2, 1, [instances], [1])
    state.data = _struct.pack(f"<{instances}h", *states)
    root.add_member("State", state)

    # ARRAY OF <struct>: one ILK_Type per instance row.
    ilks = [(1, "HiVac", 0x0041), (0, "Ok", 0x0002), (0, "", 0x0000)][:instances]
    ilk = CIPArray()
    ilk.from_instance(make_ilk_type(), 14, 1, [instances], [1])
    ilk.data = b"".join(ilk_bytes(*fields) for fields in ilks)
    root.add_member("Ilk", ilk)

    # A plain nested struct that is itself a structure-of-arrays: its members
    # are indexed by instance, exactly like Desc and State above.
    root.add_member(
        "Status",
        make_status(instances=instances, words=[0x0000, 0x002C, 0x0000][:instances]),
    )

    return root


def make_temperature_struct():
    """``DLS\\Temperature_External_Structure`` as a real PLC declares it.

    Taken from ``dls-plc-tools read <ip> -p Temp`` against a vacuum PLC: 29
    instances (``ARRAY[0..28]``), two STRING[39] arrays, nine REAL arrays, and
    the ``Status`` BOOL array that used to strand the read -- 29 bits, which
    Omron packs into 4 bytes. The controller declares the whole structure 3484
    bytes, which the member sizes add up to only once BOOL arrays are sized
    Omron's way, so this doubles as the check on that.
    """
    root = CIPStructure()
    root.variable_type_name = "DLS\\Temperature_External_Structure"
    root.size = 3484
    instances = 29

    def array(instance, element_size):
        member = CIPArray()
        member.from_instance(instance, element_size, 1, [instances], [0])
        return member

    root.add_member("Interfc", array(CIPInteger(), 2))
    root.add_member("Interface_Desc0", array(cip_string(39), 39))
    root.add_member("Desc", array(cip_string(39), 39))
    limits = ("HiHiLimit", "HiLimit", "LoLimit", "LoLoLimit")
    for limit in limits + tuple(f"{limit}_RB" for limit in limits):
        root.add_member(limit, array(CIPReal(), 4))
    root.add_member("Value", array(CIPReal(), 4))
    root.add_member("Status", array(CIPBoolean(), 2))
    root.add_member("Debounce_Time", array(CIPInteger(), 2))
    root.add_member("Debounce_Time_RB", array(CIPInteger(), 2))
    root.data = bytes(root.size)
    return root
