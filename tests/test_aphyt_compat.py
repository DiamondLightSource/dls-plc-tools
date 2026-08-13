"""The BOOL array size correction applied to aphyt.

aphyt sizes a BOOL array ``n // 8 (+2 if n % 16)``; Omron packs it into whole
16-bit words. The disagreement mis-slices every member after such an array
inside a structure -- values silently wrong, and a crash from inside the library
once the drift runs past the end of the read.
"""

from aphyt.cip.cip_datatypes import CIPArray, CIPBoolean

from dls_plc_tools import aphyt_compat  # noqa: F401  (applied on import)
from dls_plc_tools.layout import bool_array_mismatches, slots
from nested import make_temperature_struct


def bool_array(elements):
    array = CIPArray()
    array.from_instance(CIPBoolean(), 2, 1, [elements], [0])
    return array


def test_bool_arrays_are_sized_the_way_omron_packs_them():
    # 11 and 29 elements are the lengths aphyt got wrong (3 and 5 bytes); 16 and
    # 20 it happened to get right, and must stay right.
    assert [bool_array(n).size for n in (11, 16, 20, 29)] == [2, 2, 4, 4]


def test_bool_array_sizes_are_always_even():
    # An odd size also trips aphyt's "even up the offset on a member type
    # change" rule, which is where the second byte of the Temp drift came from.
    assert all(bool_array(n).size % 2 == 0 for n in range(1, 200))


def test_temperature_structure_members_fit_the_declared_size():
    """The tag that exposed this: 3484 bytes declared, 3486 consumed before the
    fix, so its last member ran off the end of a read that was complete."""
    struct = make_temperature_struct()
    member_slots = slots(struct)
    assert member_slots[-1].path == "Debounce_Time_RB"
    assert member_slots[-1].end == struct.size == 3484
    # The member that used to be sized 5 bytes, ending on an odd offset.
    status = next(slot for slot in member_slots if slot.path == "Status")
    assert (status.offset, status.size) == (3364, 4)
    assert bool_array_mismatches(struct) == []
