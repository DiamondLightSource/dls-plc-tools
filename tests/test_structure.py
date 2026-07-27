"""Flattening of structures whose members are not CIP primitives.

The failure this guards against is a nested member reaching a table cell as
aphyt's repr::

    Ilk[1] ->  { type: ILK_Type | members: {'Trip': 1, 'Msg': HiVac} }
"""

from dls_plc_tools.structure import (
    ancestors,
    branch_paths,
    cell_text,
    columns_for,
    find,
    flatten,
    row_count,
    row_text,
    type_name,
)
from nested import make_struct


def paths(columns):
    return [column.member.path for column in columns]


def member(members, path):
    """find() that fails the test rather than returning None."""
    found = find(members, path)
    assert found is not None, f"{path} not in the flattened tree"
    return found


def test_primitive_members_stay_flat_columns():
    members, start = flatten(make_struct(), "Gauge")
    assert start == 1  # members are declared ARRAY[1..3]
    desc = member(members, "Desc")
    assert not desc.branch
    assert desc.values == ["pump1", "valve2", ""]
    assert member(members, "State").values == [4, 7, 0]


def test_array_of_structs_becomes_a_branch():
    members, _ = flatten(make_struct(), "Gauge")
    ilk = member(members, "Ilk")
    assert ilk.branch
    assert ilk.type_name == "ARRAY[1..3] OF ILK_Type"
    assert [c.path for c in ilk.children] == ["Ilk.Trip", "Ilk.Msg", "Ilk.IMG"]
    # One value per instance row, lined up with the flat members' rows.
    assert member(members, "Ilk.Trip").values == [1, 0, 0]
    assert member(members, "Ilk.Msg").values == ["HiVac", "Ok", ""]


def test_nested_struct_of_arrays_stays_instance_indexed():
    """A plain nested struct is the structure-of-arrays convention one level
    down (PSU.Status.W[1]), so its arrays spread down the rows -- they must not
    collapse onto row 0 as one comma-joined cell."""
    members, _ = flatten(make_struct(), "Gauge")
    status = member(members, "Status")
    assert status.branch
    assert status.type_name == "Status_Type"
    assert member(members, "Status.W").values == [b"\x00\x00", b"\x2c\x00", b"\x00\x00"]
    assert [row_text(member(members, "Status.W"), r) for r in range(3)] == [
        "16#0000",
        "16#002C",
        "16#0000",
    ]


def test_status_words_render_as_plc_hex():
    # BYTE/WORD/DWORD arrive as raw bytes; b'A\x00' in a cell is unreadable.
    members, _ = flatten(make_struct(), "Gauge")
    assert row_text(member(members, "Ilk.IMG.W"), 0) == "16#0041"


def test_collapsed_branch_shows_a_readable_summary():
    members, _ = flatten(make_struct(), "Gauge")
    ilk = member(members, "Ilk")
    # Not aphyt's "{ type: ILK_Type | members: ... }" repr.
    assert row_text(ilk, 0).startswith("Trip=1 Msg=HiVac IMG={")
    assert row_text(ilk, 1).startswith("Trip=0 Msg=Ok IMG={")


def test_bit_array_inside_an_instance_joins_into_one_cell():
    """Gauge[x].IMG.B is one gauge's own bits, not instances, so it stays in a
    single cell -- a column per bit would add 16 columns."""
    members, _ = flatten(make_struct(), "Gauge")
    bits = member(members, "Ilk.IMG.B")
    assert not bits.branch
    # 16#0041 -> bits 0 and 6 set, index 0 leftmost, grouped in eights.
    assert row_text(bits, 0) == "10000010 00000000"


def test_collapsed_by_default_and_expands_to_leaf_columns():
    members, _ = flatten(make_struct(), "Gauge")
    collapsed = columns_for(members)
    assert paths(collapsed) == ["Desc", "State", "Ilk", "Status"]
    assert collapsed[2].header == "▸ Ilk"
    assert collapsed[2].toggle == "Ilk"
    assert collapsed[0].toggle is None  # a leaf header does not toggle

    expanded = columns_for(members, {"Ilk"})
    # The branch keeps its own summary column as the collapse affordance.
    assert paths(expanded) == [
        "Desc",
        "State",
        "Ilk",
        "Ilk.Trip",
        "Ilk.Msg",
        "Ilk.IMG",
        "Status",
    ]
    assert expanded[2].header == "▾ Ilk"


def test_expanding_everything_reaches_every_leaf():
    members, _ = flatten(make_struct(), "Gauge")
    assert branch_paths(members) == {"Ilk", "Ilk.IMG", "Status"}
    assert paths(columns_for(members, branch_paths(members))) == [
        "Desc",
        "State",
        "Ilk",
        "Ilk.Trip",
        "Ilk.Msg",
        "Ilk.IMG",
        "Ilk.IMG.W",
        "Ilk.IMG.B",
        "Status",
        "Status.W",
        "Status.Code",
    ]


def test_row_count_follows_the_array_members():
    members, _ = flatten(make_struct(instances=2), "Gauge")
    assert row_count(members) == 2


def test_type_names_are_plc_facing():
    members, _ = flatten(make_struct(), "Gauge")
    assert member(members, "Ilk.Trip").type_name == "INT"
    assert member(members, "Ilk.IMG.W").type_name == "WORD"
    assert member(members, "Ilk.IMG.B").type_name == "ARRAY[0..15] OF BOOL"
    assert member(members, "Status.W").type_name == "ARRAY[1..3] OF WORD"
    assert member(members, "Desc").type_name == "ARRAY[1..3] OF STRING"


def test_ancestors_of_a_nested_path():
    assert ancestors("Ilk.Trip") == {"Ilk"}
    assert ancestors("A.B.C") == {"A", "A.B"}
    assert ancestors("Desc") == set()


def test_cell_text_of_a_plain_value_is_not_clipped():
    # STRING[39] descriptions are the common case and must survive intact.
    long_desc = "x" * 39
    assert cell_text(long_desc) == long_desc


def test_type_name_of_unknown_object_falls_back_to_class_name():
    assert type_name(object()) == "object"
