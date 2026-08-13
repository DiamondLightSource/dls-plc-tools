"""Member layout audit for structures aphyt fails to decode.

The failure this exists for is a bare ``struct.error: unpack requires a buffer
of 2 bytes`` thrown from deep inside aphyt while decoding a whole-tag read. No
PLC is reachable here, so the two ways that happens are built by hand: bytes
short of the declared size, and a member walk that overruns them.
"""

import struct as _struct

from aphyt.cip.cip_datatypes import CIPArray, CIPBoolean, CIPStructure

from dls_plc_tools import (
    aphyt_compat,  # noqa: F401  (BOOL array sizes, applied on import)
)
from dls_plc_tools.layout import audit, bool_array_mismatches, bool_array_sizes, slots
from nested import make_struct


def data_for(struct, size):
    struct.data = bytes(size)
    return struct


def bool_array(elements):
    array = CIPArray()
    array.from_instance(CIPBoolean(), 2, 1, [elements], [0])
    return array


def test_slots_follow_aphyts_own_offsets():
    struct = make_struct()
    by_path = {slot.path: slot for slot in slots(struct)}
    # Desc is ARRAY[1..3] OF STRING[8], so State starts at 24 and Ilk at 30.
    assert (by_path["Desc"].offset, by_path["Desc"].size) == (0, 24)
    assert (by_path["State"].offset, by_path["State"].size) == (24, 6)
    assert by_path["Ilk"].offset == 30
    # Nested members are reported at their absolute offset in the parent.
    assert by_path["Status.W"].offset == by_path["Status"].offset
    assert by_path["Status.Code"].offset == by_path["Status"].offset + 6


def test_short_read_is_named_as_such():
    struct = make_struct()
    struct.size = 100
    lines = "\n".join(audit("Gauge", data_for(struct, 60)))
    assert "declares 100 bytes, read returned 60 bytes" in lines
    assert "short read: 40 bytes missing" in lines


def test_overrunning_member_is_pinpointed():
    struct = make_struct()
    struct.size = 84  # what the member walk adds up to
    # Desc and State decode; Ilk (30..72) is the first to run off the end.
    lines = "\n".join(audit("Gauge", data_for(struct, 60)))
    assert "first member past the end of the data: Ilk" in lines
    assert "<-- past end of data" in lines


def test_layout_disagreeing_with_the_controller_is_called_out():
    struct = make_struct()
    struct.size = len(_struct.pack("<h", 0)) * 4  # nothing like the member walk
    lines = "\n".join(audit("Gauge", data_for(struct, struct.size)))
    assert "layout disagrees with the controller" in lines


def test_bool_array_sizes_agree_once_aphyt_is_corrected():
    # aphyt_compat fixes the sizing at source, so nothing should be flagged;
    # this is the guard that notices if that correction ever stops applying.
    for elements in (4, 11, 16, 20, 29):
        aphyt_size, packed = bool_array_sizes(bool_array(elements))
        assert aphyt_size == packed, elements


def test_bool_array_mismatch_found_inside_a_nested_struct():
    # An uncorrected aphyt sizes an 11-element BOOL array 3 bytes rather than 2,
    # which is what mis-slices every member after it. Set by hand here, since
    # the library no longer produces it.
    bits = bool_array(11)
    bits.size = 3
    inner = CIPStructure()
    inner.variable_type_name = "Inner_Type"
    inner.add_member("Bits", bits)
    inner.size = 3
    root = CIPStructure()
    root.variable_type_name = "Root_Type"
    root.add_member("Inner", inner)
    root.size = 3
    assert bool_array_mismatches(root) == [("Inner.Bits", 3, 2)]
    assert "BOOL array Inner.Bits" in "\n".join(audit("Root", data_for(root, 3)))
